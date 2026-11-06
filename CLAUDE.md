# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A LINE group-chat bot that turns a human-curated stretch of conversation into
a GitHub issue. A user brackets messages with `ai start` / `ai end`; the
messages in between (text, photos, files) are summarized by a self-hosted
Ollama LLM into a structured draft, attachments are uploaded to a GitHub
release, an issue is filed, and the issue URL is pushed back into the LINE
chat.

It's a headless backend — no frontend, no test suite, no REST API for end
users. The only UI is LINE chat commands (`ai start` / `ai end` / `ai cancel`
/ `ai help`); the only output besides the chat reply is a GitHub issue.

**The AI integration is Ollama (`qwen3.5:9b`), not Claude/OpenAI/Anthropic.**
There is no LLM SDK in `requirements.txt` — `app/services/summarizer.py` calls
Ollama's `/api/chat` directly over HTTP with `aiohttp`. Don't assume
Anthropic/OpenAI conventions (message SDKs, tool-use blocks, etc.) apply here.

For the full architecture writeup (data model, sequence flow, deployment
topology), see [`.docs/ARCHITECTURE.md`](.docs/ARCHITECTURE.md).

## Running the app

There's no test suite, linter, or formatter configured in this repo — don't
invent commands to run them.

```bash
# Local dev setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the API service
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run the queue worker (separate process)
python -m app.workers.issue_worker

# Update dependencies
pip install <package>          # installs into .venv only
pip freeze > requirements.txt  # snapshot deps
```

Both processes need a `.env` file (see `.env.example` for every variable,
documented per-line) and reach Postgres, RabbitMQ, and Ollama as configured
in `app/core/config.py`.

Docker: `docker-compose.yml` builds `api` and `issue-worker` from the **same
image** (`Dockerfile`) — only the `command:` differs. `Dockerfile.consumer`
is a near-duplicate, currently unused by `docker-compose.yml`.

## Architecture

Two long-running processes, sharing one Postgres DB and one media volume, decoupled by a RabbitMQ queue:

- **`api`** ([`app/main.py`](app/main.py)) — FastAPI. Receives LINE webhooks, logs every message, manages capture sessions, publishes jobs to the queue when a capture closes.
- **`issue-worker`** ([`app/workers/issue_worker.py`](app/workers/issue_worker.py)) — a hand-rolled `pika` (RabbitMQ) consumer, **not Celery/RQ**. Consumes one job at a time (`prefetch_count=1`), summarizes via Ollama, uploads attachments, files the GitHub issue, replies to LINE.

Request flow: `ai start` → webhook → `dispatcher.py` opens a `capture_sessions` row → every subsequent message logged to `context_messages` → `ai end` → dispatcher closes the session, claims an `issue_requests` row, publishes `{issue_request_id, capture_session_id, conversation_id}` to queue `line.issue` → `issue_worker.process_job()` summarizes + files the issue + pushes the result back to LINE.

Key modules:
- [`app/line/dispatcher.py`](app/line/dispatcher.py) — LINE event handling and the `ai start/end/cancel/help` command parser. The command regex requires the *whole* message to match (`^ai[\s,]+(start|end|cancel|help)[\s.!?]*$`) so ordinary chatter like "ai end of the month" doesn't trigger anything.
- [`app/line/controllers.py`](app/line/controllers.py) — all DB access. Raw SQL via `psycopg_pool`, **no ORM** (this was a deliberate move away from one — see git history).
- [`app/line/schema.sql`](app/line/schema.sql) — DDL, applied idempotently by `ensure_schema()` on every process boot. Tables: `webhook_events`, `context_messages`, `capture_sessions`, `issue_requests`, `line_profiles`.
- [`app/services/summarizer.py`](app/services/summarizer.py) — the Ollama integration. JSON-schema-constrained output, `think=False` (deliberate latency tradeoff), a `warmup()` call fired at process startup to avoid cold-start latency on the first real request.
- [`app/services/attachments.py`](app/services/attachments.py) + [`github_client.py`](app/services/github_client.py) — uploads captured images/files to a GitHub *release* as assets (GitHub has no native issue-attachment API), then links them into the issue body.
- [`app/queue/publisher.py`](app/queue/publisher.py) — RabbitMQ publish, sync + `asyncio.to_thread`-wrapped async variant.

## Non-obvious constraints worth knowing before editing

- **`issue_requests.status` is a state machine** (`pending → summarized → done/failed`) used for idempotency. `issue_worker.process_job()` checks for `status == "done"` before doing any work, because RabbitMQ redelivery on a lost ack must not re-file a duplicate GitHub issue.
- **Failures are terminal, not retried.** Any exception in `process_job()` marks the request `failed`, notifies the LINE group, and `nack`s with `requeue=False`. The fix path is the human re-recording with `ai start`, not automatic retry — redelivering would just burn the LLM again on the same failure.
- **`heartbeat=0` on the RabbitMQ connection is deliberate**, not an oversight — a single job can block the worker's `BlockingConnection` thread for minutes (Ollama call, plus a possible truncation retry), and a blocked thread can't service AMQP heartbeats. The default 60s heartbeat was killing the connection mid-job and causing duplicate runs.
- **One open capture session per conversation**, enforced by a partial unique index (`ux_capture_open_per_conv`), not application logic — don't try to "fix" concurrent `ai start`/`ai end` races in Python; the DB already resolves them.
- **Attachment downloads are fire-and-forget asyncio tasks** in the API process, held alive via a module-level `_background_tasks` set (asyncio only holds weak references to tasks otherwise — an ungrounded task can be GC'd mid-download). The worker polls `attachment_status` with a capped wait (`attachment_wait_s`) rather than blocking indefinitely.
- **No fallback summarization.** If a capture session has zero messages, the worker raises rather than summarizing "recent chat" instead — the user picked these messages by hand.
- **Two-machine deployment**: Machine A runs `api` + `issue-worker` + Postgres + Ollama; Machine B runs RabbitMQ + nginx (TLS termination, reverse proxy). Containers reach host services via `host.docker.internal`. See `.docs/ARCHITECTURE.md` §6 for the full topology.
- [`app/line/consumer.py`](app/line/consumer.py) is a legacy/unused entry point (a separate "PVSD bot" LINE-push consumer), explicitly marked "Not ON right now" in comments and not wired into `docker-compose.yml`. Don't assume it's part of the active pipeline.
