from datetime import datetime

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.db import pool
from app.line.models import CaptureSession, ContextMessage

# ---------------------------------------------------------------------------
# webhook_events — event-level dedupe (LINE retries webhooks)
# ---------------------------------------------------------------------------

RECORD_EVENT_SQL = """
INSERT INTO webhook_events (webhook_event_id, event_type, is_redelivery)
VALUES (%(webhook_event_id)s, %(event_type)s, %(is_redelivery)s)
ON CONFLICT (webhook_event_id) DO NOTHING
RETURNING 1;
"""

def record_webhook_event(webhook_event_id: str, event_type: str, is_redelivery: bool) -> bool:
    """Return True if this is a new event, False if it's a duplicate"""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                RECORD_EVENT_SQL,
                {
                    "webhook_event_id": webhook_event_id,
                    "event_type": event_type,
                    "is_redelivery": is_redelivery,
                },
            )
            return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# context_messages
# ---------------------------------------------------------------------------

INSERT_MESSAGE_SQL = """
INSERT INTO context_messages (
    line_message_id, webhook_event_id, conversation_type, conversation_id,
    sender_id, sender_name, message_type, text, quoted_message_id, quote_token,
    attachment_name, attachment_bytes, attachment_status, line_ts
) VALUES (
    %(line_message_id)s, %(webhook_event_id)s, %(conversation_type)s, %(conversation_id)s,
    %(sender_id)s, %(sender_name)s, %(message_type)s, %(text)s, %(quoted_message_id)s, %(quote_token)s,
    %(attachment_name)s, %(attachment_bytes)s, %(attachment_status)s, %(line_ts)s
)
ON CONFLICT (line_message_id) DO NOTHING
RETURNING id, created_at;
"""


def insert_context_message(message: ContextMessage) -> ContextMessage | None:
    """Returns the message with id/cread_at populated, or None if it was
    already logged (ON CONFLICT DO NOTHING - duplicate delivery)."""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                INSERT_MESSAGE_SQL,
                {
                    "line_message_id": message.line_message_id,
                    "webhook_event_id": message.webhook_event_id,
                    "conversation_type": message.conversation_type,
                    "conversation_id": message.conversation_id,
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "message_type": message.message_type,
                    "text": message.text,
                    "quoted_message_id": message.quoted_message_id,
                    "quote_token": message.quote_token,
                    "attachment_name": message.attachment_name,
                    "attachment_bytes": message.attachment_bytes,
                    "attachment_status": message.attachment_status,
                    "line_ts": message.line_ts,
                }
            )
            row = cur.fetchone()
            if row is None:
                return None
            message.id, message.created_at = row[0], row[1]
    return message


UPDATE_ATTACHMENT_SQL = """
UPDATE context_messages
SET attachment_path = %(path)s,
    attachment_preview_path = %(preview_path)s,
    attachment_mime = %(mime)s,
    attachment_status = %(status)s
WHERE line_message_id = %(line_message_id)s;
"""


def update_attachment(
    line_message_id: str,
    *,
    path: str | None,
    preview_path: str | None,
    mime: str | None,
    status: str,
) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                UPDATE_ATTACHMENT_SQL,
                {
                    "line_message_id": line_message_id,
                    "path": path,
                    "preview_path": preview_path,
                    "mime": mime,
                    "status": status,
                },
            )


MARK_UNSENT_SQL = """
UPDATE context_messages SET is_unsent = TRUE WHERE line_message_id = %(line_message_id)s;
"""


def mark_message_unsent(line_message_id: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_UNSENT_SQL, {"line_message_id": line_message_id})


# ---------------------------------------------------------------------------
# capture_sessions — the "ai start" ... "ai end" recording
# ---------------------------------------------------------------------------

_SESSION_COLUMNS = """
    id, conversation_id, conversation_type, started_by, start_message_id, start_ts,
    end_message_id, end_ts, status, created_at
"""


# Someone types "ai start", gets distracted, and never ends it. Left alone
# that session would still be open days later and swallow every unrelated
# message since. Expiring it on the next "ai start" keeps the stale one from
# blocking a fresh recording.
EXPIRE_STALE_SESSIONS_SQL = """
UPDATE capture_sessions
SET status = 'expired'
WHERE conversation_id = %(conversation_id)s
  AND status = 'open'
  AND start_ts < now() - make_interval(mins => %(ttl_minutes)s)
RETURNING id;
"""


# No ON CONFLICT target: this can collide either on start_message_id (a
# reprocessed webhook) or on the partial "one open session per conversation"
# index, and both mean the same thing — don't insert, go look at what's there.
INSERT_SESSION_SQL = f"""
INSERT INTO capture_sessions (
    conversation_id, conversation_type, started_by, start_message_id, start_ts
) VALUES (
    %(conversation_id)s, %(conversation_type)s, %(started_by)s, %(start_message_id)s, %(start_ts)s
)
ON CONFLICT DO NOTHING
RETURNING {_SESSION_COLUMNS};
"""


SELECT_OPEN_SESSION_SQL = f"""
SELECT {_SESSION_COLUMNS} FROM capture_sessions
WHERE conversation_id = %(conversation_id)s AND status = 'open';
"""


def open_capture_session(
    conversation_id: str,
    conversation_type: str,
    started_by: str | None,
    start_message_id: str,
    start_ts: datetime,
    ttl_minutes: int,
) -> tuple[CaptureSession, bool] | None:
    """Opens a recording for this conversation.

    Returns (session, True) when a fresh one was opened, (session, False) when
    one was already running (the caller should say so rather than silently
    restarting — restarting would throw away whatever is already captured), or
    None if neither could be established.
    """
    with pool.connection() as conn:
        with conn.cursor(row_factory=class_row(CaptureSession)) as cur:
            cur.execute(
                EXPIRE_STALE_SESSIONS_SQL,
                {"conversation_id": conversation_id, "ttl_minutes": ttl_minutes},
            )
            cur.execute(
                INSERT_SESSION_SQL,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": conversation_type,
                    "started_by": started_by,
                    "start_message_id": start_message_id,
                    "start_ts": start_ts,
                },
            )
            row = cur.fetchone()
            if row is not None:
                return row, True

            cur.execute(SELECT_OPEN_SESSION_SQL, {"conversation_id": conversation_id})
            existing = cur.fetchone()
            return (existing, False) if existing is not None else None


# The UPDATE itself is the lock: "WHERE status = 'open'" matches exactly one
# row, so two simultaneous "ai end" messages produce one closed session and
# one None — no double issue.
_FINISH_SESSION_SQL = f"""
UPDATE capture_sessions
SET status = %(status)s, end_message_id = %(end_message_id)s, end_ts = %(end_ts)s
WHERE conversation_id = %(conversation_id)s AND status = 'open'
RETURNING {_SESSION_COLUMNS};
"""


def _finish_capture_session(
    conversation_id: str, end_message_id: str, end_ts: datetime, status: str
) -> CaptureSession | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=class_row(CaptureSession)) as cur:
            cur.execute(
                _FINISH_SESSION_SQL,
                {
                    "conversation_id": conversation_id,
                    "end_message_id": end_message_id,
                    "end_ts": end_ts,
                    "status": status,
                },
            )
            return cur.fetchone()


def close_capture_session(
    conversation_id: str, end_message_id: str, end_ts: datetime
) -> CaptureSession | None:
    """Closes the open recording. None means there wasn't one."""
    return _finish_capture_session(conversation_id, end_message_id, end_ts, "closed")


def cancel_capture_session(
    conversation_id: str, end_message_id: str, end_ts: datetime
) -> CaptureSession | None:
    """Abandons the open recording without building an issue."""
    return _finish_capture_session(conversation_id, end_message_id, end_ts, "cancelled")


MARK_SESSION_EMPTY_SQL = """
UPDATE capture_sessions SET status = 'empty' WHERE id = %(id)s;
"""


def mark_capture_session_empty(session_id: int) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_SESSION_EMPTY_SQL, {"id": session_id})


# The captured span, resolved from the marker messages' own (line_ts, id)
# positions. Both bounds are strict, which is what keeps "ai start" and
# "ai end" themselves out of the summary. Ordering by (line_ts, id): LINE
# timestamps are millisecond-resolution and DO collide on rapid-fire
# messages, so id breaks ties by ingest order. end_id IS NULL (session still
# open, or the end marker somehow never landed) means "everything since the
# start marker".
_SESSION_BOUNDS_CTE = """
WITH s AS (
    SELECT conversation_id, start_message_id, end_message_id
    FROM   capture_sessions
    WHERE  id = %(session_id)s
),
bounds AS (
    SELECT s.conversation_id,
           st.line_ts AS start_ts, st.id AS start_id,
           en.line_ts AS end_ts,   en.id AS end_id
    FROM        s
    JOIN        context_messages st ON st.line_message_id = s.start_message_id
    LEFT JOIN   context_messages en ON en.line_message_id = s.end_message_id
)
"""

_IN_SESSION_PREDICATE = """
    m.conversation_id = b.conversation_id
    AND NOT m.is_unsent
    AND (m.line_ts, m.id) > (b.start_ts, b.start_id)
    AND (b.end_id IS NULL OR (m.line_ts, m.id) < (b.end_ts, b.end_id))
"""

SELECT_SESSION_MESSAGES_SQL = f"""
{_SESSION_BOUNDS_CTE}
SELECT m.*
FROM   context_messages m, bounds b
WHERE {_IN_SESSION_PREDICATE}
ORDER  BY m.line_ts ASC, m.id ASC
LIMIT  %(limit)s;
"""

COUNT_SESSION_MESSAGES_SQL = f"""
{_SESSION_BOUNDS_CTE}
SELECT count(*)
FROM   context_messages m, bounds b
WHERE {_IN_SESSION_PREDICATE};
"""

COUNT_PENDING_ATTACHMENTS_SQL = f"""
{_SESSION_BOUNDS_CTE}
SELECT count(*)
FROM   context_messages m, bounds b
WHERE {_IN_SESSION_PREDICATE}
  AND  m.attachment_status = 'pending';
"""


def get_capture_messages(session_id: int, limit: int) -> list[ContextMessage]:
    """Everything the user bracketed, oldest first. Truncates to the *oldest*
    `limit` messages: a runaway recording is still anchored on the problem
    being reported, which is what was said first."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=class_row(ContextMessage)) as cur:
            cur.execute(SELECT_SESSION_MESSAGES_SQL, {"session_id": session_id, "limit": limit})
            return cur.fetchall()


def count_capture_messages(session_id: int) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(COUNT_SESSION_MESSAGES_SQL, {"session_id": session_id})
            return cur.fetchone()[0]


def count_pending_attachments(session_id: int) -> int:
    """Images still being downloaded by the API process. The worker waits on
    this so a photo sent just before "ai end" still makes it into the
    summary."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(COUNT_PENDING_ATTACHMENTS_SQL, {"session_id": session_id})
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# issue_requests — the idempotent claim around AI-summarize + GitHub publish
# ---------------------------------------------------------------------------


INSERT_ISSUE_REQUEST_SQL = """
INSERT INTO issue_requests (
    capture_session_id, conversation_id, requested_by, message_count
) VALUES (
    %(capture_session_id)s, %(conversation_id)s, %(requested_by)s, %(message_count)s
)
ON CONFLICT (capture_session_id) DO NOTHING
RETURNING id;
"""


SELECT_ISSUE_REQUEST_SQL = """
SELECT id, status, issue_url
FROM issue_requests
WHERE capture_session_id = %(capture_session_id)s;
"""


SELECT_ISSUE_REQUEST_BY_ID_SQL = """
SELECT status, issue_url FROM issue_requests WHERE id = %(id)s;
"""


def get_issue_request_status(req_id: int) -> tuple[str, str | None] | None:
    """(status, issue_url) for one request, or None if it's gone. Used by the
    worker to spot a redelivered job before redoing the whole publish."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_ISSUE_REQUEST_BY_ID_SQL, {"id": req_id})
            return cur.fetchone()


SELECT_ISSUE_REQUEST_REQUESTER_SQL = """
SELECT requested_by FROM issue_requests WHERE id = %(id)s;
"""


def get_issue_request_requester(req_id: int) -> str | None:
    """LINE sender_id of whoever sent 'ai end' for this request — the person
    who actually triggered issue creation, not necessarily the one who
    reported the problem in the captured messages."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_ISSUE_REQUEST_REQUESTER_SQL, {"id": req_id})
            row = cur.fetchone()
            return row[0] if row else None


RECLAIM_ONE_STALE_SQL = """
UPDATE issue_requests
SET claimed_at = now(), status = 'pending', error = NULL
WHERE id = %(id)s
  AND status = 'pending'
  AND claimed_at < now() - make_interval(mins => %(stale_minutes)s)
RETURNING id;
"""


def claim_issue_request(
    capture_session_id: int,
    conversation_id: str,
    requested_by: str | None,
    message_count: int,
) -> tuple[int | None, str | None, str | None]:
    """Atomically claims an issue request for this capture session.

    Returns (claimed_id, existing_status, existing_issue_url):
      - (id, None, None)       -> freshly claimed, go ahead and summarize
      - (None, 'done', url)    -> already published; re-post `url`, don't recreate
      - (None, 'pending'/'summarized'/'failed', url) -> in flight or dead; caller decides
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                INSERT_ISSUE_REQUEST_SQL,
                {
                    "capture_session_id": capture_session_id,
                    "conversation_id": conversation_id,
                    "requested_by": requested_by,
                    "message_count": message_count,
                },
            )
            row = cur.fetchone()
            if row is not None:
                return row[0], None, None

            cur.execute(
                SELECT_ISSUE_REQUEST_SQL,
                {"capture_session_id": capture_session_id},
            )
            existing = cur.fetchone()
            if existing is None:
                return None, None, None
            req_id, status, issue_url = existing

            if status == "pending":
                cur.execute(
                    RECLAIM_ONE_STALE_SQL,
                    {"id": req_id, "stale_minutes": settings.stale_claim_minutes},
                )
                reclaimed = cur.fetchone()
                if reclaimed is not None:
                    return reclaimed[0], None, None

            return None, status, issue_url


MARK_SUMMARIZED_SQL = """
UPDATE issue_requests SET status = 'summarized', summary_json = %(summary)s WHERE id = %(id)s;
"""


def mark_issue_summarized(req_id: int, summary: dict) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_SUMMARIZED_SQL, {"id": req_id, "summary": Jsonb(summary)})


MARK_DONE_SQL = """
UPDATE issue_requests
SET status = 'done', github_issue_number = %(number)s, issue_url = %(url)s, completed_at = now()
WHERE id = %(id)s;
"""


def mark_issue_done(req_id: int, number: int, url: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_DONE_SQL, {"id": req_id, "number": number, "url": url})


# `status <> 'done'` guards a published issue: if the GitHub call succeeded and
# something *after* it threw, the request is done — recording it as 'failed'
# would lose the issue_url and invite a duplicate on the next trigger.
MARK_FAILED_SQL = """
UPDATE issue_requests
SET status = 'failed', error = %(error)s, completed_at = now()
WHERE id = %(id)s AND status <> 'done';
"""


def mark_issue_failed(req_id: int, error: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_FAILED_SQL, {"id": req_id, "error": error})


RECLAIM_STALE_SQL = """
UPDATE issue_requests
SET claimed_at = now()
WHERE status = 'pending' AND claimed_at < now() - make_interval(mins => %(minutes)s)
RETURNING id, capture_session_id, conversation_id, requested_by;
"""


def reclaim_stale_requests(minutes: int) -> list[dict]:
    """Startup sweep: finds issue_requests stuck in 'pending' (worker crashed
    mid-job) and resets their claim so they can be re-published to the queue."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=class_row(dict)) as cur:
            cur.execute(RECLAIM_STALE_SQL, {"minutes": minutes})
            return cur.fetchall()


# ---------------------------------------------------------------------------
# line_profiles — cached display names (GroupSource.user_id is optional and
# profile lookups cost an API call, so cache on first resolution)
# ---------------------------------------------------------------------------

SELECT_DISPLAY_NAME_SQL = """
SELECT display_name FROM line_profiles WHERE conversation_id = %(conversation_id)s AND user_id = %(user_id)s;
"""


def get_cached_display_name(conversation_id: str, user_id: str) -> str | None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_DISPLAY_NAME_SQL, {"conversation_id": conversation_id, "user_id": user_id})
            row = cur.fetchone()
            return row[0] if row else None


UPSERT_DISPLAY_NAME_SQL = """
INSERT INTO line_profiles (conversation_id, user_id, display_name)
VALUES (%(conversation_id)s, %(user_id)s, %(display_name)s)
ON CONFLICT (conversation_id, user_id)
DO UPDATE SET display_name = EXCLUDED.display_name, fetched_at = now();
"""


def upsert_display_name(conversation_id: str, user_id: str, display_name: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                UPSERT_DISPLAY_NAME_SQL,
                {"conversation_id": conversation_id, "user_id": user_id, "display_name": display_name},
            )