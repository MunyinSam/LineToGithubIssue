# AI Context Summary -> Github Draft Issue
import json
import logging
import re

import aiohttp

from app.core.config import settings
from app.line.models import ContextMessage, IssueDraft

logger = logging.getLogger(__name__)



class SummarizerError(Exception):
    pass



class SummarizerTruncated(SummarizerError):
    """Raised when Ollama hit num_predict before finishing the JSON object.
    Caller should retry with a smaller context window, not a bigger num_predict
    blindly — a 9B model repeating itself on a long window is the usual cause."""


SYSTEM_PROMPT = (
    "You are a triage assistant for a LINE support group. You will be shown a "
    "batch of chat messages that a human help-desk agent selected by hand: "
    "they typed 'ai start', collected the relevant messages (some sent "
    "directly, some forwarded from elsewhere), then typed 'ai end'. Every "
    "message shown was deliberately included; images and files are noted by "
    "name only and are attached to the issue separately, not shown to you. "
    "Write a single, concrete GitHub issue describing the problem these "
    "messages report. Be specific: quote exact error text/values from the "
    "text, and do not invent details not present in the conversation. "
    "Forwarded messages may jump between topics or times — cover the one "
    "problem they have in common. If information is missing, say so in the "
    "body rather than guessing. "
    "Answer Only in Thai Language. English is only allowed in certain words that only existed in English"
)

# The key list is spelled out even though `format` already carries the schema,
# because with think=false Ollama 0.30.10 stops enforcing `format` on this
# model — measured: schema / "json" / no format all returned ```json-fenced
# output with invented keys ("issue", "issue_title", "description"), while
# this instruction gave valid, correctly-keyed JSON on every run and cut the
# call from ~55-195s to ~7s. `format` stays set so enforcement resumes free
# if a later Ollama fixes the interaction.
FINAL_INSTRUCTION = (
    "Now produce the GitHub issue for the messages above. Return ONLY a raw "
    "JSON object — no markdown code fences, no commentary before or after — "
    "with exactly these keys: title, body, severity, steps_to_reproduce, "
    "expected, actual, labels, confidence."
)

ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "steps_to_reproduce": {"type": "array", "items": {"type": "string"}},
        "expected": {"type": "string"},
        "actual": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["title", "body", "severity", "expected", "actual"],
    "additionalProperties": False,
}


# Ollama's `format` grammar normally guarantees a bare JSON object, but that
# guarantee is not absolute across model/option combinations — a ```json fence
# around otherwise-good output should cost a retry, not the whole issue.
# Leading/trailing fences are stripped independently (not as one atomic
# start-to-end match): the issue body is markdown and can contain its own
# ``` code spans, which broke a single-match regex whenever the model's
# closing fence wasn't literally the last three characters of the response.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_json(content: str) -> dict:
    stripped = _FENCE_RE.sub("", content or "").strip()
    try:
        # strict=False: this model occasionally emits a literal control
        # character (e.g. a raw newline) inside a string value instead of the
        # JSON-escaped \n. Strict json.loads rejects that outright even
        # though the object is otherwise well-formed.
        return json.loads(stripped, strict=False)
    except json.JSONDecodeError:
        # Last resort: the model added commentary before/after the object
        # despite being told not to. Slice to the outermost braces and retry
        # once rather than failing the whole issue over stray prose.
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1], strict=False)


def _coerce_str_list(value) -> list[str]:
    """steps_to_reproduce/labels are schema'd as arrays, but this model has
    returned a bare string for them instead. render_issue_body then does
    `enumerate(draft.steps_to_reproduce)` — a string is iterable too, so that
    silently exploded into one numbered "step" per *character*. Wrap a lone
    string as a single-item list instead of letting it iterate that way."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _render_line(m: ContextMessage) -> str:
    ts = m.line_ts.strftime("%H:%M")
    who = m.sender_name or m.sender_id or "unknown"

    if m.message_type == "text":
        body = m.text or ""
    elif m.message_type == "image":
        body = "[image attached]"
    elif m.message_type == "file":
        body = f"[file attached: {m.attachment_name or 'unknown'}]"
    elif m.message_type == "sticker":
        body = "[sticker]"
    elif m.message_type == "location":
        body = "[location]"
    else:
        body = f"[{m.message_type}]"

    return f"[{ts}] {who}: {body}"


def render_window(msgs: list[ContextMessage]) -> list[dict]:
    """Builds the messages[] array for /api/chat: one text block with the
    whole capture rendered as a transcript. Images/files aren't decoded or
    sent to the model — the capture is hand-picked by a human already, and
    the attachments are linked onto the issue directly (see
    app.services.attachments), so the model only needs the words."""
    lines = [_render_line(m) for m in msgs]
    return [{"role": "user", "content": "\n".join(lines)}]


async def summarize(msgs: list[ContextMessage]) -> IssueDraft:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(render_window(msgs))
    messages.append({"role": "user", "content": FINAL_INSTRUCTION})

    payload = {
        "model": settings.chat_model,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "messages": messages,
        "format": ISSUE_SCHEMA,
        "think": settings.ollama_think,
        "options": {
            "temperature": 0.2,
            "num_ctx": settings.ollama_num_ctx,
            "num_predict": settings.ollama_num_predict,
        },
    }

    timeout = aiohttp.ClientTimeout(total=settings.ollama_timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{settings.ai_api}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
    
    if data.get("done_reason") == "length":
        raise SummarizerTruncated("Ollama hit num_predict before finishing the JSON object")

    content = data.get("message", {}).get("content", "")
    try:
        parsed = _parse_json(content)
    except json.JSONDecodeError as e:
        raise SummarizerError(f"Ollama did not return valid JSON: {e!r}. content={content[:500]!r}") from e

    missing = {"title", "body"} - parsed.keys()
    if missing:
        raise SummarizerError(
            f"Ollama returned JSON without {sorted(missing)}: keys={sorted(parsed)} "
            f"content={content[:300]!r}"
        )

    try:
        # IssueDraft is a plain dataclass with no type coercion, and this
        # model isn't reliably schema-enforced (see FINAL_INSTRUCTION above)
        # — it has returned confidence as a quoted string, which then blew up
        # the ":.2f" format in render_issue_body far from this call site.
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return IssueDraft(
        title=parsed["title"],
        body=parsed["body"],
        severity=parsed.get("severity", "medium"),
        expected=parsed.get("expected", ""),
        actual=parsed.get("actual", ""),
        steps_to_reproduce=_coerce_str_list(parsed.get("steps_to_reproduce")),
        labels=_coerce_str_list(parsed.get("labels")),
        confidence=confidence,
    )


async def warmup() -> None:
    """Fires a tiny call with the exact num_ctx/keep_alive used in production
    so the first real trigger doesn't eat the ~60s cold model load."""
    payload = {
        "model": settings.chat_model,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "messages": [{"role": "user", "content": "ping"}],
        "think": settings.ollama_think,
        "options": {"num_ctx": settings.ollama_num_ctx, "num_predict": 8},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{settings.ai_api}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                await resp.json()
        logger.info("Ollama warmup complete (model=%s, num_ctx=%s)", settings.chat_model, settings.ollama_num_ctx)
    except Exception:
        logger.exception("Ollama warmup failed (non-fatal, first real request will pay the cold-load cost)")
