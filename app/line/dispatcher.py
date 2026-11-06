import asyncio
import logging
import re
from datetime import datetime, timezone

from linebot.v3.webhooks import (
    AudioMessageContent,
    Event,
    FileMessageContent,
    GroupSource,
    ImageMessageContent,
    JoinEvent,
    MessageEvent,
    RoomSource,
    StickerMessageContent,
    TextMessageContent,
    UnsendEvent,
    UserSource,
    VideoMessageContent,
)

from app.core.config import settings
from app.line import controllers as db
from app.line.media import download_file, download_image, guess_mime
from app.line.models import ContextMessage
from app.queue.publisher import publish_async
from app.services.notifier import fetch_display_name, reply_ack

logger = logging.getLogger(__name__)

# asyncio only holds weak references to running tasks, so a fire-and-forget
# download can be garbage-collected mid-flight — leaving attachment_status
# stuck at 'pending' and the image missing from the summary. Hold a strong
# reference until the task completes.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def conversation_of(source) -> tuple[str, str] | None:
    if isinstance(source, GroupSource):
        return "group", source.group_id
    if isinstance(source, RoomSource):
        return "room", source.room_id
    if isinstance(source, UserSource):
        return "user", source.user_id
    return None


# The capture commands. The LINE mention picker doesn't reliably offer the
# Official Account, so the bot is driven by plain text; an @-mention in front
# is stripped first, so "@Bot ai end" works too. The command has to be the
# *whole* message — that's what keeps ordinary chatter like "ai end of the
# month" or "ai starts tomorrow" from firing anything.
_COMMAND_RE = re.compile(r"^ai[\s,]+(start|end|cancel|help)[\s.!?]*$", re.IGNORECASE)


def parse_command(msg: TextMessageContent) -> str | None:
    """'start' | 'end' | 'cancel' | 'help', or None for an ordinary message."""
    text = strip_mentions(msg.text or "", msg.mention)
    match = _COMMAND_RE.match(text.strip())
    return match.group(1).lower() if match else None


def strip_mentions(text: str, mention) -> str:
    """Removes @-mention spans from message text (by index/length), in
    reverse order so removing a later span doesn't shift the indices of
    earlier ones."""
    if mention is None:
        return text
    spans = sorted(mention.mentionees, key=lambda m: m.index, reverse=True)
    for m in spans:
        text = text[: m.index] + text[m.index + m.length :]
    return text.strip()


def _message_type_of(content) -> str:
    if isinstance(content, TextMessageContent):
        return "text"
    if isinstance(content, ImageMessageContent):
        return "image"
    if isinstance(content, FileMessageContent):
        return "file"
    if isinstance(content, VideoMessageContent):
        return "video"
    if isinstance(content, AudioMessageContent):
        return "audio"
    if isinstance(content, StickerMessageContent):
        return "sticker"
    return getattr(content, "type", "unknown")


def _to_dt(ms_timestamp: int) -> datetime:
    return datetime.fromtimestamp(ms_timestamp / 1000.0, tz=timezone.utc)


async def handle_event(event: Event) -> None:
    if isinstance(event, MessageEvent):
        await _handle_message_event(event) # MAIN CASE
    elif isinstance(event, JoinEvent):
        conv = conversation_of(event.source)
        logger.info("Bot joined conversation %s", conv)
    elif isinstance(event, UnsendEvent):
        db.mark_message_unsent(event.unsend.message_id)
    else:
        logger.debug("Ignoring event type %s", type(event).__name__)


async def _handle_message_event(event: MessageEvent) -> None:
    """
    Receive Message Content -> Logs to DB
    Receive Image -> Save using _download_sync() func

    If the message is an "ai start"/"ai end"/"ai cancel" command ->
    _handle_command()
    """
    conv = conversation_of(event.source)
    if conv is None:
        logger.warning("MessageEvent with unrecgnized source: %r", event.source)
        return
    conversation_type, conversation_id = conv
    content = event.message
    message_type = _message_type_of(content)
    line_ts = _to_dt(event.timestamp)
    sender_id = getattr(event.source, "user_id", None)

    # Have Content
    text = None
    quoted_message_id = None
    quote_token = None
    if isinstance(content, TextMessageContent):
        text = content.text
        quoted_message_id = content.quoted_message_id
        quote_token = content.quote_token

    # Have File
    attachment_name = None
    attachment_bytes = None
    if isinstance(content, FileMessageContent):
        attachment_name = content.file_name
        attachment_bytes = content.file_size
    
    sender_name = await _resolve_sender_name(conversation_type, conversation_id, sender_id)

    msg = ContextMessage(
        line_message_id=content.id,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        message_type=message_type,
        line_ts=line_ts,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        quoted_message_id=quoted_message_id,
        quote_token=quote_token,
        attachment_name=attachment_name,
        attachment_bytes=attachment_bytes,
        attachment_status="pending" if message_type in ("image", "file") else "none",
        webhook_event_id=event.webhook_event_id,
    )
    inserted = db.insert_context_message(msg)
    if inserted is None:
        logger.info("Duplicate message %s, already logged", content.id)
        return

    if message_type == "image":
        # async so it does not stop the process
        _spawn(_download_and_update(content.id, conversation_id, line_ts))
    elif message_type == "file":
        # Same deal as images: LINE's blob expires, so a PDF/Word/Excel has to
        # be pulled now or it's unrecoverable by the time the worker runs.
        _spawn(_download_file_and_update(content.id, conversation_id, line_ts, attachment_name))
    
    # The command message is logged like any other first — that's deliberate.
    # A capture is bounded by the marker messages' own rows, so "ai start" has
    # to exist in context_messages before the session can point at it.
    if isinstance(content, TextMessageContent):
        command = parse_command(content)
        if command is not None:
            await _handle_command(
                command, event, conversation_type, conversation_id, sender_id, content.id, line_ts
            )


async def _resolve_sender_name(
    conversation_type: str, conversation_id: str, user_id: str | None
) -> str | None:
    """Cache-first display-name lookup. Without this the summarizer only ever
    sees opaque 'U1a2b3c...' IDs, which makes the generated issues much vaguer."""
    if user_id is None:
        return None
    cached = db.get_cached_display_name(conversation_id, user_id)
    if cached is not None:
        return cached
    name = await fetch_display_name(conversation_type, conversation_id, user_id)
    if name is not None:
        db.upsert_display_name(conversation_id, user_id, name)
    return name


async def _download_and_update(message_id: str, conversation_id: str, line_ts: datetime) -> None:
    path, preview_path, status = await download_image(message_id, conversation_id, line_ts)
    mime = "image/jpeg" if status == "stored" else None
    db.update_attachment(message_id, path=path, preview_path=preview_path, mime=mime, status=status)


async def _download_file_and_update(
    message_id: str, conversation_id: str, line_ts: datetime, file_name: str | None
) -> None:
    path, preview_path, status = await download_file(message_id, conversation_id, line_ts, file_name)
    # LINE gives a filename but no content type, so the extension is all we
    # have to go on — and it's what the GitHub upload sends as Content-Type.
    mime = guess_mime(file_name) if status == "stored" else None
    db.update_attachment(message_id, path=path, preview_path=preview_path, mime=mime, status=status)


USAGE = (
    "How to log an issue:\n"
    "1. Send: ai start\n"
    "2. Send or forward every message and photo about the problem\n"
    "3. Send: ai end\n"
    "I'll turn everything in between into a GitHub issue. "
    "Send 'ai cancel' to throw a recording away."
)


async def _handle_command(
    command: str,
    event: MessageEvent,
    conversation_type: str,
    conversation_id: str,
    sender_id: str | None,
    message_id: str,
    line_ts: datetime,
) -> None:
    if command == "start":
        await _start_capture(event, conversation_type, conversation_id, sender_id, message_id, line_ts)
    elif command == "end":
        await _end_capture(event, conversation_id, sender_id, message_id, line_ts)
    elif command == "cancel":
        await _cancel_capture(event, conversation_id, message_id, line_ts)
    else:
        await reply_ack(event.reply_token, USAGE)


async def _start_capture(
    event: MessageEvent,
    conversation_type: str,
    conversation_id: str,
    sender_id: str | None,
    message_id: str,
    line_ts: datetime,
) -> None:
    result = db.open_capture_session(
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        started_by=sender_id,
        start_message_id=message_id,
        start_ts=line_ts,
        ttl_minutes=settings.capture_session_ttl_minutes,
    )
    if result is None:
        logger.warning("Could not open a capture session in %s", conversation_id)
        await reply_ack(event.reply_token, "Couldn't start recording — try 'ai start' again.")
        return

    session, created = result
    if created:
        await reply_ack(
            event.reply_token,
            "Recording. Send or forward everything about the problem, then send 'ai end'.",
        )
        return

    # Restarting would silently discard whatever is already captured, so say
    # what's going on instead. Elapsed minutes rather than a clock time: LINE
    # timestamps are UTC and the group isn't.
    elapsed = int((datetime.now(timezone.utc) - session.start_ts).total_seconds() // 60)
    await reply_ack(
        event.reply_token,
        f"Already recording (started {elapsed} min ago). "
        "Send 'ai end' to finish it, or 'ai cancel' to drop it.",
    )


async def _end_capture(
    event: MessageEvent,
    conversation_id: str,
    sender_id: str | None,
    message_id: str,
    line_ts: datetime,
) -> None:
    """
    Close the recording -> Claim the issue request -> Reply ack to LINE ->
    Publish the job to RabbitMQ
    """
    session = db.close_capture_session(conversation_id, message_id, line_ts)
    if session is None:
        await reply_ack(event.reply_token, "Nothing is being recorded right now.\n\n" + USAGE)
        return

    count = db.count_capture_messages(session.id)
    if count == 0:
        db.mark_capture_session_empty(session.id)
        await reply_ack(
            event.reply_token,
            "There was nothing between 'ai start' and 'ai end', so there's nothing to log.",
        )
        return

    claimed_id, existing_status, existing_url = db.claim_issue_request(
        session.id, conversation_id, sender_id, count
    )
    if claimed_id is None:
        if existing_status == "done" and existing_url:
            await reply_ack(event.reply_token, f"Already logged: {existing_url}")
        elif existing_status == "failed":
            await reply_ack(
                event.reply_token,
                "That one failed earlier — record it again with 'ai start' to retry.",
            )
        else:
            await reply_ack(event.reply_token, "Already working on that one — hang tight.")
        return

    plural = "" if count == 1 else "s"
    await reply_ack(event.reply_token, f"Got it — building an issue from {count} message{plural}…")
    await publish_async(
        settings.issue_queue_name,
        {
            "issue_request_id": claimed_id,
            "capture_session_id": session.id,
            "conversation_id": conversation_id,
        },
    )


async def _cancel_capture(
    event: MessageEvent, conversation_id: str, message_id: str, line_ts: datetime
) -> None:
    session = db.cancel_capture_session(conversation_id, message_id, line_ts)
    if session is None:
        await reply_ack(event.reply_token, "Nothing is being recorded right now.")
        return
    await reply_ack(event.reply_token, "Dropped that recording. Send 'ai start' to begin a new one.")