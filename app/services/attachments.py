# Captured images/files -> GitHub release assets (durable backup) + this
# app's own /line/attachments route (inline preview) -> links in the issue body
import logging
import re
from urllib.parse import quote

from app.core.config import settings
from app.line.media import DEFAULT_MIME, read_bytes
from app.line.models import ContextMessage, IssueAttachment

logger = logging.getLogger(__name__)

# GitHub rewrites anything outside this set in an asset name, so do it here
# instead and keep the name predictable enough to look up again on a retry.
_SAFE_ASSET = re.compile(r"[^A-Za-z0-9._-]+")


def _asset_name(m: ContextMessage) -> str:
    """Asset names must be unique within the release, so prefix with the LINE
    message id — globally unique, and stable across a redelivered job. Thai
    filenames sanitize down to almost nothing, which is why the readable name
    is kept separately for the link text."""
    raw = m.attachment_name or f"{m.message_type}.jpg"
    safe = _SAFE_ASSET.sub("-", raw).strip("-.") or "attachment"
    return f"{m.line_message_id}--{safe}"[:180]


def _display_name(m: ContextMessage) -> str:
    if m.attachment_name:
        return m.attachment_name
    who = m.sender_name or "unknown"
    return f"image from {who} at {m.line_ts:%H:%M}"


def collect(msgs: list[ContextMessage]) -> list[IssueAttachment]:
    """Everything in the capture that has bytes on disk, in conversation
    order. Messages whose download expired or failed are surfaced with a note
    rather than dropped silently — 'the PDF is missing' is information the
    person triaging the issue needs."""
    found: list[IssueAttachment] = []
    for m in msgs:
        if m.message_type not in ("image", "file"):
            continue
        if m.attachment_status != "stored" or not m.attachment_path:
            found.append(
                IssueAttachment(
                    display_name=_display_name(m),
                    asset_name=_asset_name(m),
                    mime=m.attachment_mime or DEFAULT_MIME,
                    note=f"not saved ({m.attachment_status})",
                )
            )
            continue
        found.append(
            IssueAttachment(
                display_name=_display_name(m),
                asset_name=_asset_name(m),
                mime=m.attachment_mime or DEFAULT_MIME,
                rel_path=m.attachment_path,
                size_bytes=m.attachment_bytes,
            )
        )

    if len(found) > settings.max_attachments_per_issue:
        dropped = len(found) - settings.max_attachments_per_issue
        logger.warning("Capture has %s attachments, linking the first %s", len(found), settings.max_attachments_per_issue)
        found = found[: settings.max_attachments_per_issue]
        found.append(IssueAttachment(display_name=f"…and {dropped} more", asset_name="", mime="", note="not uploaded (per-issue limit)"))
    return found


async def publish(msgs: list[ContextMessage]) -> list[IssueAttachment]:
    """Uploads each captured file and fills in its URL.

    Never raises: a failed upload must not cost the user the issue itself, so
    the failure becomes a note in the attachment list and the issue is filed
    anyway. Import is local to keep this module usable without a GitHub token
    in tests.
    """
    from app.services.github_client import upload_release_asset

    attachments = collect(msgs)
    if not settings.attach_files_to_issues:
        for a in attachments:
            if a.rel_path is not None:
                a.note = "captured, not uploaded (attachments disabled)"
        return attachments

    for a in attachments:
        if a.rel_path is None:
            continue
        try:
            data = read_bytes(a.rel_path)
        except OSError:
            logger.exception("Attachment %s vanished from disk", a.rel_path)
            a.note = "not uploaded (missing on disk)"
            continue

        if len(data) > settings.github_asset_max_bytes:
            a.note = f"not uploaded (too large: {len(data) / 1_048_576:.1f} MB)"
            logger.warning("Skipping %s: %s bytes exceeds github_asset_max_bytes", a.asset_name, len(data))
            continue

        # The issue body links here for inline preview: GitHub renders
        # `![]()` from any URL that serves real image bytes, but a Releases
        # `browser_download_url` doesn't behave like one. Releases still get
        # the upload below as the durable copy.
        a.url = f"{settings.public_base_url}/line/attachments/{quote(a.rel_path)}"

        try:
            await upload_release_asset(a.asset_name, data, a.mime)
        except Exception:
            logger.exception("Failed to upload backup copy of attachment %s to GitHub Releases", a.asset_name)

    uploaded = sum(1 for a in attachments if a.url)
    if attachments:
        logger.info("Uploaded %s of %s attachment(s)", uploaded, len(attachments))
    return attachments
