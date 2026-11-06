import asyncio
import logging
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path

from linebot.v3.messaging import ApiClient, Configuration, MessagingApiBlob
from linebot.v3.messaging.exceptions import ApiException, NotFoundException

from app.core.config import settings

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_-]")

DEFAULT_MIME = "application/octet-stream"


def _sanitize(value: str) -> str:
    return _SAFE.sub("_", value)


def file_extension(file_name: str | None) -> str:
    """Extension for the on-disk copy. Only used for the stored filename — the
    real type travels in attachment_mime, which is what the upload sends."""
    ext = Path(file_name or "").suffix.lstrip(".").lower()
    return _SAFE.sub("", ext)[:16] or "bin"


def guess_mime(file_name: str | None) -> str:
    """Best-effort Content-Type for an uploaded asset. LINE's file messages
    carry a filename but no MIME type, so the extension is all there is."""
    if not file_name:
        return DEFAULT_MIME
    return mimetypes.guess_type(file_name)[0] or DEFAULT_MIME


def media_path(
    conversation_id: str, line_message_id: str, line_ts: datetime, ext: str, preview: bool = False
) -> tuple[str, str]:
    """Returns (absolute_path, path_relative_to_media_root)."""
    suffix = f".preview.{ext}" if preview else f".{ext}"
    rel = os.path.join(
        _sanitize(conversation_id),
        f"{line_ts.year:04d}",
        f"{line_ts.month:02d}",
        f"{line_ts.day:02d}",
        f"{_sanitize(line_message_id)}{suffix}",
    )
    abs_path = os.path.join(settings.media_root, rel)
    return abs_path, rel


def _write_atomic(abs_path: str, content: bytes) -> None:
    Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
    part_path = f"{abs_path}.part"
    with open(part_path, "wb") as f:
        f.write(content)
    os.replace(part_path, abs_path)


def _get_blob_client() -> MessagingApiBlob:
    configuration = Configuration(access_token=settings.line_channel_access_token)
    return MessagingApiBlob(ApiClient(configuration))


def _download_sync(message_id: str, conversation_id: str, line_ts: datetime) -> tuple[str | None, str | None, str]:
    blob_api = _get_blob_client()
    try:
        original = blob_api.get_message_content(message_id)
    except NotFoundException:
        logger.warning("Attachment expired for message %s", message_id)
        return None, None, "expired"
    except ApiException:
        logger.exception("Failed to download attachment for message %s", message_id)
        return None, None, "failed"

    abs_path, rel_path = media_path(conversation_id, message_id, line_ts, "jpg", preview=False)
    try:
        _write_atomic(abs_path, bytes(original))
    except OSError:
        logger.exception("Failed to write attachment to disk for message %s", message_id)
        return None, None, "failed"

    return rel_path, None, "stored"


async def download_image(
    message_id: str, conversation_id: str, line_ts: datetime
) -> tuple[str | None, str | None, str]:
    """Downloads the full-resolution image. The summarizer no longer does
    vision (captures are hand-picked by a human already, so the image is
    just attached to the issue, not read) — hence no preview-sized fetch
    here anymore. Never raises; returns a status string so a failed download
    doesn't block the message row that's already been inserted."""
    return await asyncio.to_thread(_download_sync, message_id, conversation_id, line_ts)


def _download_file_sync(
    message_id: str, conversation_id: str, line_ts: datetime, file_name: str | None
) -> tuple[str | None, str | None, str]:
    blob_api = _get_blob_client()
    try:
        content = blob_api.get_message_content(message_id)
    except NotFoundException:
        logger.warning("Attachment expired for message %s", message_id)
        return None, None, "expired"
    except ApiException:
        logger.exception("Failed to download attachment for message %s", message_id)
        return None, None, "failed"

    abs_path, rel_path = media_path(
        conversation_id, message_id, line_ts, file_extension(file_name), preview=False
    )
    try:
        _write_atomic(abs_path, bytes(content))
    except OSError:
        logger.exception("Failed to write attachment to disk for message %s", message_id)
        return None, None, "failed"

    return rel_path, None, "stored"


async def download_file(
    message_id: str, conversation_id: str, line_ts: datetime, file_name: str | None
) -> tuple[str | None, str | None, str]:
    """PDF/Word/Excel and friends come from the same blob endpoint as images
    and expire just as fast, so they have to be pulled at ingest or they're
    gone for good. There is no preview variant for a file, hence the None in
    the middle of the tuple — the shape matches download_image so the two
    share a single update path."""
    return await asyncio.to_thread(_download_file_sync, message_id, conversation_id, line_ts, file_name)


def read_bytes(rel_path: str) -> bytes:
    abs_path = os.path.join(settings.media_root, rel_path)
    with open(abs_path, "rb") as f:
        return f.read()
