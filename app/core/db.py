import logging
from pathlib import Path

from psycopg_pool import ConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

# app/core/db.py -> app/line/schema.sql
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "line" / "schema.sql"

pool = ConnectionPool(
    conninfo=settings.pgvector_conninfo,
    min_size=settings.pgvector_pool_min_size,
    max_size=settings.pgvector_pool_max_size,
    open=False,
)

def open_pool() -> None:
    pool.open()
    logger.info("DB pool opened (%s-%s)", settings.pgvector_pool_min_size, settings.pgvector_pool_max_size)


def close_pool() -> None:
    pool.close()
    logger.info("DB pool closed")


def ensure_schema() -> None:
    """Apply schema.sql. Idempotent (CREATE TABLE/INDEX IF NOT EXISTS)."""
    sql = _SCHEMA_PATH.read_text()
    with pool.connection() as conn:
        conn.execute(sql)
    logger.info("Schema ensured from %s", _SCHEMA_PATH)