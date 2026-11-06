import asyncio
import json
import logging
import time

import pika

from app.core.config import settings
from app.core.db import close_pool, ensure_schema, open_pool
from app.line import controllers as db
from app.line.models import IssueAttachment, IssueDraft
from app.queue.publisher import publish
from app.services.attachments import publish as publish_attachments
from app.services.github_client import GithubClientError, create_issue
from app.services.notifier import push_result
from app.services.summarizer import SummarizerError, SummarizerTruncated, summarize, warmup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def render_issue_body(
    draft: IssueDraft, attachments: list[IssueAttachment] | None = None, reporter: str | None = None
) -> str:
    lines = []
    if reporter:
        lines.append(f"**Reported by:** {reporter}")
        lines.append("")
    lines.append(draft.body)
    lines.append("")
    if draft.steps_to_reproduce:
        lines.append("**Steps to reproduce:**")
        for i, step in enumerate(draft.steps_to_reproduce, 1):
            lines.append(f"{i}. {step}")
        lines.append("")
    lines.append(f"**Expected:** {draft.expected}")
    lines.append(f"**Actual:** {draft.actual}")

    if attachments:
        lines.append("")
        lines.append("**Attachments:**")
        for a in attachments:
            # `a.url` points at this app's own /line/attachments route, not
            # GitHub, so `![]()` actually inline-previews — a GitHub Releases
            # browser_download_url never renders as an image regardless of
            # markdown syntax.
            if a.url:
                lines.append(f"- ![{a.display_name}]({a.url})")
            else:
                lines.append(f"- {a.display_name} — _{a.note or 'unavailable'}_")

    lines.append("")
    lines.append(f"_Auto-generated from a LINE conversation (confidence: {draft.confidence:.2f})_")
    return "\n".join(lines)


async def _await_attachments(session_id: int) -> None:
    """Images are downloaded by background tasks in the *API* process, so a
    capture that ends right after a photo can reach this worker before the
    JPEG is on disk — and a photo the user deliberately included is exactly
    what the summary needs. Poll the rows the API updates, with a hard cap so
    a download that never finishes can't stall the queue."""
    deadline = time.monotonic() + settings.attachment_wait_s
    while True:
        pending = db.count_pending_attachments(session_id)
        if pending == 0:
            return
        if time.monotonic() >= deadline:
            logger.warning(
                "Summarizing session %s without %s attachment(s) still downloading",
                session_id,
                pending,
            )
            return
        await asyncio.sleep(1)


async def process_job(job: dict) -> None:
    """
    Get Job from queue -> Retrieve the captured messages from DB -> Summarize
    using LocalLLM -> Mark Issue as Summarized in DB -> Create Github Issue ->
    Mark as done in DB -> Send logs messages to LINE
    """
    req_id = job["issue_request_id"]
    conversation_id = job["conversation_id"]
    session_id = job["capture_session_id"]

    # A redelivered job — the broker never saw our ack — must not summarize and
    # publish all over again. Without this one trigger produces two GitHub
    # issues: the first run's issue, then a second identical one.
    already = db.get_issue_request_status(req_id)
    if already is not None and already[0] == "done":
        logger.info("issue_request %s already published (%s) — skipping redelivery", req_id, already[1])
        return

    await _await_attachments(session_id)

    # Everything the user bracketed with "ai start" / "ai end". No fallback to
    # recent chat if it comes back empty: they picked these messages by hand,
    # so summarizing something else would be worse than failing loudly.
    msgs = db.get_capture_messages(session_id, limit=settings.max_capture_messages)
    if not msgs:
        raise RuntimeError(f"Capture session {session_id} has no messages")

    try:
        draft = await summarize(msgs)
    except SummarizerTruncated:
        # Only worth retrying if the window actually gets smaller. The old
        # `msgs[-max(4, len(msgs) // 2):]` returned the whole list for any
        # capture of 4 or fewer messages, so a short capture retried the
        # identical prompt and failed the identical way — twice the latency,
        # no second chance.
        trimmed = msgs[-max(4, len(msgs) // 2) :]
        if len(trimmed) >= len(msgs):
            logger.warning("Summary truncated for req %s and the window can't be trimmed further", req_id)
            raise
        logger.warning(
            "Summary truncated for req %s, retrying once with %s of %s messages",
            req_id,
            len(trimmed),
            len(msgs),
        )
        draft = await summarize(trimmed)


    db.mark_issue_summarized(
        req_id,
        {
            "title": draft.title,
            "body": draft.body,
            "severity": draft.severity,
            "expected": draft.expected,
            "actual": draft.actual,
            "steps_to_reproduce": draft.steps_to_reproduce,
            "labels": draft.labels,
            "confidence": draft.confidence,
        },
    )

    # Upload before filing: the body has to carry the asset URLs. publish()
    # swallows its own failures, so a broken upload costs links, not the issue.
    attachments = await publish_attachments(msgs)

    # Whoever sent "ai end" claimed this issue_request — that's a reliable,
    # non-guessed identity, unlike relying on the LLM to spot who's reporting
    # the problem inside the captured chat text.
    requester_id = db.get_issue_request_requester(req_id)
    reporter = db.get_cached_display_name(conversation_id, requester_id) if requester_id else None

    # Files the issue under the reporter's own mapped GitHub account, if they
    # have one, so the account shown as its creator matches whoever actually
    # ran "ai end". Anyone unmapped (or with no cached display name) falls
    # back to the shared default token.
    token = settings.github_token_by_name.get(reporter) if reporter else None

    labels = sorted({*draft.labels, settings.github_auto_label})
    number, url = await create_issue(
        draft.title, render_issue_body(draft, attachments, reporter), labels, token=token
    )
    db.mark_issue_done(req_id, number, url)
    await push_result(conversation_id, f"Logged: {url}", dedupe_key=f"issue-done:{req_id}")


def on_message(channel, method, properties, body):
    try:
        job = json.loads(body)
    except json.JSONDecodeError:
        # Unparseable payload will never succeed on redelivery — drop it.
        logger.exception("Discarding malformed job payload: %r", body[:500])
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    req_id = job.get("issue_request_id")
    try:
        asyncio.run(process_job(job))
    except Exception as e:
        # Catch-all, not just the three domain errors: anything escaping into
        # pika's callback tears down the consumer, and with prefetch_count=1 an
        # unacked message would stall the worker forever.
        logger.exception("Job failed permanently (req %s): %s", req_id, e)
        if req_id is not None:
            db.mark_issue_failed(req_id, str(e))
        try:
            asyncio.run(
                push_result(
                    job.get("conversation_id", ""),
                    "Sorry, I couldn't build that issue. Record it again with 'ai start' to retry.",
                    dedupe_key=f"issue-failed:{req_id}",
                )
            )
        except Exception:
            logger.exception("Also failed to notify the group about the failure")
        # requeue=False: the failure is recorded in issue_requests.status and
        # the user was told to re-record. Redelivering would just burn the LLM
        # again.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    # Acked outside the job's try/except on purpose. An ack that fails is a
    # broker/connection problem, not a failed job — folding it into the handler
    # above is what marked a published issue 'failed' and announced it as one.
    channel.basic_ack(delivery_tag=method.delivery_tag)


def _requeue_stale_jobs() -> None:
    """Startup sweep: an issue_request stuck in 'pending' means a previous
    worker process claimed it and then crashed mid-job. Re-publish it so
    the job actually gets retried instead of silently vanishing."""
    stale = db.reclaim_stale_requests(settings.stale_claim_minutes)
    for req in stale:
        logger.warning("Re-publishing stale issue_request %s after a worker crash/restart", req["id"])
        publish(
            settings.issue_queue_name,
            {
                "issue_request_id": req["id"],
                "capture_session_id": req["capture_session_id"],
                "conversation_id": req["conversation_id"],
            },
        )


def main():
    open_pool()
    # Lets the worker come up against a fresh DB without waiting for the API.
    ensure_schema()
    _requeue_stale_jobs()
    try:
        asyncio.run(warmup())
    except Exception:
        logger.exception("Ollama warmup failed at worker startup (non-fatal)")

    params = pika.URLParameters(settings.rabbitmq_url)
    # heartbeat=0 disables AMQP heartbeats for this consumer. A job blocks this
    # thread for minutes (the LLM call, plus a second one on the truncation
    # retry) and a BlockingConnection cannot send heartbeats while that runs —
    # so the broker's default 60s heartbeat killed the connection mid-job,
    # requeued the unacked message, and ran the entire job a second time.
    # Nothing else shares this connection, so TCP is enough to notice a dead
    # peer here.
    params.heartbeat = 0
    connection = pika.BlockingConnection(params)
    try:
        channel = connection.channel()
        channel.queue_declare(queue=settings.issue_queue_name, durable=True)
        # One job at a time: each one holds the local LLM for up to ollama_timeout_s.
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=settings.issue_queue_name, on_message_callback=on_message)

        logger.info("issue-worker waiting on queue '%s'...", settings.issue_queue_name)
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            channel.stop_consuming()
    finally:
        connection.close()
        close_pool()


if __name__ == "__main__":
    main()