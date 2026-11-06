# Line Webhook
import logging
import os

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError

from app.core.config import settings
from app.core.db import pool
from app.line import controllers as db
from app.line.dispatcher import handle_event
from app.line.media import guess_mime, read_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/line", tags=["line"])
_parser = WebhookParser(settings.line_channel_secret)



@router.post("/webhook")
async def line_webhook(request: Request) -> PlainTextResponse:
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        # The SDK's validator calls signature.encode() unguarded, so a missing
        # header would surface as a 500 + traceback. Anything scanning the
        # public URL hits this, so answer cleanly instead.
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = (await request.body()).decode("utf-8")

    try:
        payload = _parser.parse(body, signature, as_payload=True)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in payload.events:
        is_redelivery = bool(event.delivery_context and event.delivery_context.is_redelivery)
        is_new = db.record_webhook_event(event.webhook_event_id, event.type, is_redelivery)
        if not is_new:
            logger.info("Skipping duplicate webhook_event_id=%s", event.webhook_event_id)
            continue
        try:
            await handle_event(event)
        except Exception:
            logger.exception("Failed to handle event %s", event.webhook_event_id)

    return PlainTextResponse("OK")


@router.get("/attachments/{rel_path:path}")
async def get_attachment(rel_path: str) -> Response:
    """Serves a captured image/file inline so it can be embedded with
    `![]()` in a GitHub issue body — GitHub only inline-renders a markdown
    image if fetching the URL returns real image bytes, and a GitHub Releases
    `browser_download_url` doesn't. `rel_path` is the same unguessable,
    per-message path `media.media_path()` already writes to disk (conversation
    id + date + LINE message id), so knowing it is the access control."""
    # media_path() only ever emits normalized relative segments, but this
    # value arrives from a public URL, so refuse ".." outright rather than
    # trust it.
    normalized = os.path.normpath(rel_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise HTTPException(status_code=404)

    try:
        data = read_bytes(normalized)
    except OSError:
        raise HTTPException(status_code=404)

    return Response(content=data, media_type=guess_mime(rel_path))


@router.get("/healthz")
async def healthz():
    """
    Check DB and AI Connection
    """
    db_ok = True
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        logger.exception("healthz: DB check failed")
        db_ok = False

    ollama_ok = True
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{settings.ai_api}/api/tags") as resp:
                ollama_ok = resp.status == 200
    except Exception:
        logger.exception("healthz: Ollama check failed")
        ollama_ok = False

    return {"db": db_ok, "ollama": ollama_ok}