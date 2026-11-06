# Main App + Line Router
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.db import close_pool, ensure_schema, open_pool
from app.line import controllers as db
from app.line.routes import router as line_router
from app.queue.publisher import publish_async
from app.services.summarizer import warmup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _requeue_stale_jobs() -> None:
    """
    In case the API restarts while a job is stuck at pending
    Re-publish all the pending
    """
    stale = db.reclaim_stale_requests(settings.stale_claim_minutes)
    for req in stale:
        logger.warning("Re-publishing stale issue_request %s after a restart", req["id"])
        await publish_async(
            settings.issue_queue_name,
            {
                "issue_request_id": req["id"],
                "capture_session_id": req["capture_session_id"],
                "conversation_id": req["conversation_id"],
            },
        )
    
@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    ensure_schema()
    await _requeue_stale_jobs()
    # AI needs a warmup since it will be idle
    warmup_task = asyncio.create_task(warmup())
    app.state.warmup_task = warmup_task

    try:
        yield
    finally:
        # finally, not a bare trailing call: an exception propagating out of the
        # app is re-raised *at* the yield, which would otherwise leak the pool.
        close_pool()


app = FastAPI(title="TRD_LineLog", lifespan=lifespan)
app.include_router(line_router)


@app.get("/")
async def root():
    return {"service": "TRD_LineLog", "status": "ok"}