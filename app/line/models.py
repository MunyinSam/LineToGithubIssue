from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ContextMessage:
    line_message_id: str
    conversation_type: str  # 'group' | 'room' | 'user'
    conversation_id: str
    message_type: str  # text|image|file|video|audio|sticker|location
    line_ts: datetime
    sender_id: str | None = None
    sender_name: str | None = None
    text: str | None = None
    quoted_message_id: str | None = None
    quote_token: str | None = None
    attachment_path: str | None = None
    attachment_preview_path: str | None = None
    attachment_mime: str | None = None
    attachment_name: str | None = None
    attachment_bytes: int | None = None
    attachment_status: str = "none"  # none|pending|stored|expired|failed
    is_unsent: bool = False
    webhook_event_id: str | None = None
    id: int | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class CaptureSession:
    """One "ai start" ... "ai end" recording. The bounds are the marker
    messages themselves; the captured messages are everything strictly
    between them (so neither marker ends up in the summary)."""

    id: int
    conversation_id: str
    conversation_type: str
    started_by: str | None
    start_message_id: str
    start_ts: datetime
    end_message_id: str | None = None
    end_ts: datetime | None = None
    status: str = "open"  # open|closed|cancelled|expired|empty
    created_at: datetime | None = None


@dataclass(slots=True)
class IssueAttachment:
    """One captured image/file on its way into the issue body. `url` is filled
    in once it's uploaded; `note` explains itself when it isn't."""

    display_name: str
    asset_name: str
    mime: str
    rel_path: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    note: str | None = None


@dataclass(slots=True)
class IssueDraft:
    title: str
    body: str
    severity: str
    expected: str
    actual: str
    steps_to_reproduce: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    confidence: float = 0.0
