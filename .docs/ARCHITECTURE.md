# TRD_LineLog — Architecture & Flow

## 1. Overview

TRD_LineLog is a LINE group-chat bot that turns a hand-picked stretch of
conversation into a GitHub issue. A human brackets the relevant messages with
`ai start` / `ai end`; everything in between (text, photos, files) is
summarized by a self-hosted LLM into a structured issue draft, attachments are
uploaded to GitHub, an issue is filed, and the resulting issue URL is pushed
back into the LINE chat.

It is a headless backend system — there is no frontend and no REST API for
end users. The only "UI" is LINE chat commands, and the only output surface
besides the chat reply is a GitHub issue.

Despite the current branch being named `feat/1/redo-ai-chat-logic`, the AI
integration is **not** Claude or OpenAI. It's a self-hosted **Ollama** server
running `qwen3.5:9b`, called directly over HTTP from
[`app/services/summarizer.py`](../app/services/summarizer.py).

## 2. Components

| Component | File(s) | Responsibility |
|---|---|---|
| **API service** (`api`) | [`app/main.py`](../app/main.py) | FastAPI app. Owns the LINE webhook, opens the DB pool, ensures schema, requeues stale jobs, warms up Ollama on boot. |
| **LINE routes** | [`app/line/routes.py`](../app/line/routes.py) | `POST /line/webhook` — verifies HMAC signature, dedupes events, dispatches to the handler. `GET /line/attachments/{rel_path}` — serves a captured image/file inline (no `Content-Disposition`) so GitHub can fetch and inline-render it in an issue body; unguessable per-message path is the only access control. `GET /line/healthz` — DB + Ollama liveness (blocked externally at nginx). |
| **Dispatcher** | [`app/line/dispatcher.py`](../app/line/dispatcher.py) | Core LINE event logic: logs every message, parses `ai start/end/cancel/help` commands, opens/closes capture sessions, claims issue requests, publishes jobs to the queue. |
| **DB controllers** | [`app/line/controllers.py`](../app/line/controllers.py) | All raw-SQL data access (no ORM) — capture sessions, context messages, issue requests, display-name cache. |
| **Media downloader** | [`app/line/media.py`](../app/line/media.py) | Downloads LINE images/files to the shared media volume before LINE's blob URLs expire. |
| **Queue publisher** | [`app/queue/publisher.py`](../app/queue/publisher.py) | Publishes jobs to RabbitMQ (`pika`), sync + async (`asyncio.to_thread`) variants. |
| **Issue worker** (`issue-worker`) | [`app/workers/issue_worker.py`](../app/workers/issue_worker.py) | Standalone RabbitMQ consumer — the core pipeline job. Summarizes, uploads attachments, files the GitHub issue, replies to LINE. |
| **Summarizer** | [`app/services/summarizer.py`](../app/services/summarizer.py) | Calls Ollama `/api/chat` with a JSON-schema-constrained prompt to produce a structured `IssueDraft`. |
| **Attachments** | [`app/services/attachments.py`](../app/services/attachments.py) | Orchestrates uploading captured images/files as GitHub release assets. |
| **GitHub client** | [`app/services/github_client.py`](../app/services/github_client.py) | REST calls to create the issue and upload release assets. |
| **Notifier** | [`app/services/notifier.py`](../app/services/notifier.py) | Sends LINE reply/push messages, resolves display names. |
| **Config** | [`app/core/config.py`](../app/core/config.py) | `pydantic-settings` `Settings`, loaded from `.env`. |
| **DB pool/schema** | [`app/core/db.py`](../app/core/db.py) | `psycopg_pool.ConnectionPool`; `ensure_schema()` applies `schema.sql` idempotently at startup. |

`api` and `issue-worker` are built from the **same Docker image**
(`Dockerfile` / `Dockerfile.consumer` are near-identical) — only the
`command:` differs between them in `docker-compose.yml`.

A third, currently-unused entry point exists:
[`app/line/consumer.py`](../app/line/consumer.py), a legacy "PVSD bot"
LINE-push consumer, explicitly marked "Not ON right now" and not wired into
`docker-compose.yml`.

## 3. Data model

PostgreSQL, accessed via raw SQL through `psycopg_pool` — no ORM. Schema:
[`app/line/schema.sql`](../app/line/schema.sql), applied idempotently by
`ensure_schema()` on every process boot.

| Table | Purpose | Key lifecycle field |
|---|---|---|
| `webhook_events` | Dedupe table — LINE retries webhook deliveries | keyed by `webhook_event_id` |
| `context_messages` | Every LINE message ever seen (text/image/file/video/audio/sticker/location), with sender, quote links, attachment metadata | `attachment_status`: `none → pending → stored/expired/failed` |
| `capture_sessions` | One row per `ai start` … `ai end` recording, bounded by the marker messages' own ids | `status`: `open → closed/cancelled/expired/empty`; unique partial index enforces one open session per conversation |
| `issue_requests` | Idempotent claim/state machine for one capture → GitHub issue | `status`: `pending → summarized → done/failed` |
| `line_profiles` | Cached display name per `(conversation_id, user_id)` | — |

Note: config carries `pgvector_*`-named settings (reusing an existing
pgvector Postgres instance's connection details), but this schema uses no
vector extension — there is no embedding/semantic search in this flow.

## 4. End-to-end flow (happy path)

1. **`ai start`** — a group message arrives at `POST /line/webhook`
   ([`routes.py`](../app/line/routes.py)). The signature is verified, the
   event deduped by `webhook_event_id`, and handed to
   `dispatcher.handle_event()`.
2. `dispatcher._handle_message_event()` logs the message into
   `context_messages`, recognizes the `ai start` command
   (`parse_command`, [`dispatcher.py:59-66`](../app/line/dispatcher.py#L59-L66)),
   and `_start_capture()` opens a `capture_sessions` row, replying via the
   LINE reply API.
3. **Every message sent afterward** is logged to `context_messages`. Images
   and files are downloaded asynchronously
   (`_spawn(_download_and_update(...))`) into the shared `media` volume, with
   `attachment_status` tracked in the DB so the worker can later tell whether
   a download is still in flight.
4. **`ai end`** — `dispatcher._end_capture()`
   ([`dispatcher.py:290-339`](../app/line/dispatcher.py#L290-L339)) closes the
   session, counts captured messages (bailing out with "nothing to log" if
   empty), atomically claims an `issue_requests` row (guards against a double
   `ai end` race), replies "building an issue…", and **publishes a job** —
   `{issue_request_id, capture_session_id, conversation_id}` — to the
   RabbitMQ queue `line.issue` via `publish_async()`.
5. **`issue-worker`** ([`app/workers/issue_worker.py`](../app/workers/issue_worker.py))
   consumes the job in `process_job()`:
   - **Idempotency check** — if `issue_requests.status == "done"`, skip (a
     redelivered message must not double-file the issue).
   - **`_await_attachments()`** — polls the DB up to `attachment_wait_s`
     (default 20s) for any images/files still downloading in the API process.
   - **`db.get_capture_messages()`** — fetches everything between the
     `ai start`/`ai end` markers.
   - **`summarize(msgs)`** ([`summarizer.py`](../app/services/summarizer.py))
     — calls Ollama's `/api/chat` with a JSON-schema-constrained prompt to
     produce an `IssueDraft` (title, body, severity, expected, actual, steps,
     labels, confidence). On `SummarizerTruncated`, retries once with a
     trimmed message window.
   - **`db.mark_issue_summarized()`** persists the draft JSON.
   - **`publish_attachments(msgs)`** ([`attachments.py`](../app/services/attachments.py))
     builds each attachment's issue-body URL from this app's own
     `/line/attachments` route (so GitHub can inline-render it with `![]()`)
     and, as a durable backup, uploads the same bytes to a GitHub release as
     an asset — done *before* filing the issue so the issue body can link
     them.
   - **`create_issue()`** ([`github_client.py`](../app/services/github_client.py))
     files the GitHub issue via REST, body rendered by `render_issue_body()`
     with reporter, steps, expected/actual, and attachment links.
   - **`db.mark_issue_done()`** records the issue number/URL.
   - **`push_result()`** sends a LINE push message back into the group with
     the issue link.

## 5. Failure & resilience behavior

- **Terminal failures, not retries.** `on_message()` in `issue_worker.py`
  catches all exceptions, marks `issue_requests.status = 'failed'`, notifies
  the LINE group ("record it again with 'ai start'"), and `nack`s with
  `requeue=False`. A failed AI/GitHub call is not silently retried — the
  human re-records instead, since blindly redelivering would just burn the
  LLM again on the same failure.
- **Stale-job recovery.** Both `api` (on FastAPI `lifespan` startup) and
  `issue-worker` (in `main()`) run a `_requeue_stale_jobs()` sweep: any
  `issue_requests` row stuck in `pending` (a previous worker claimed it and
  crashed mid-job) is re-published to the queue.
- **Single in-flight job, no AMQP heartbeat.** The consumer sets
  `prefetch_count=1` (one job at a time — each can hold the Ollama connection
  up to `ollama_timeout_s`, default 180s) and disables AMQP heartbeats
  (`heartbeat=0`). A synchronous `BlockingConnection` can't service
  heartbeats while blocked on a multi-minute LLM call; without this, the
  broker's default 60s heartbeat would kill the connection mid-job and
  requeue (and re-run) the same job.
- **Attachment download is best-effort, not blocking.** Images/files are
  downloaded via fire-and-forget asyncio tasks in the API process (with
  strong references held in `_background_tasks` so they aren't
  garbage-collected mid-flight). The worker waits up to `attachment_wait_s`
  and proceeds without a slow download rather than stalling the queue
  indefinitely.
- **No fallback summarization.** If a capture session has zero messages, the
  worker raises rather than guessing — the user picked these messages by
  hand, so summarizing something else would be worse than failing loudly.

## 6. Deployment topology

Two machines, per [`.env.example`](../.env.example) and `deploy/`:

**Machine A** — runs the application:
- `api` and `issue-worker` containers ([`docker-compose.yml`](../docker-compose.yml)), sharing a `media` Docker volume.
- PostgreSQL (existing host instance, not containerized here).
- Ollama (existing host instance, not containerized here).
- Both containers reach Postgres/Ollama via `host.docker.internal`.

**Machine B** — runs shared infrastructure:
- RabbitMQ broker ([`deploy/rabbitmq/docker-compose.yml`](../deploy/rabbitmq/docker-compose.yml), `rabbitmq:4-management-alpine`), AMQP bound to the LAN IP only, management UI reachable only via SSH tunnel (loopback).
- nginx ([`deploy/nginx/linelog.conf`](../deploy/nginx/linelog.conf)) — TLS termination and reverse proxy, forwards `/line/*` to Machine A's FastAPI app on port 8000, preserves the raw request body (required for HMAC signature verification), and explicitly blocks external access to `/line/healthz`.

```
Internet → nginx (Machine B, TLS) → FastAPI api (Machine A)
                                          │
                                          ├─→ Postgres (Machine A host)
                                          ├─→ media volume (shared with issue-worker)
                                          └─→ RabbitMQ (Machine B) ←── issue-worker (Machine A)
                                                                            │
                                                                            ├─→ Ollama (Machine A host)
                                                                            ├─→ GitHub REST API
                                                                            └─→ LINE Messaging API (push reply)
```

## 7. Configuration

All settings load from `.env` via `pydantic-settings`
([`app/core/config.py`](../app/core/config.py)), documented per-variable in
[`.env.example`](../.env.example). Groups: LINE credentials, RabbitMQ
URL/queue name, Postgres connection, Ollama (model, context size, timeouts),
capture-session tuning (TTL, message caps), GitHub (token, owner/repo,
labels, asset upload), storage (`media_root`), and `stale_claim_minutes`.

## 8. A note on "AI chat logic"

The AI integration lives entirely in
[`app/services/summarizer.py`](../app/services/summarizer.py):

- Self-hosted **Ollama**, model `qwen3.5:9b`, called directly over HTTP
  (`aiohttp`) — no LangChain, no OpenAI-compatible SDK, no Anthropic SDK.
- A `SYSTEM_PROMPT` instructs the model to act as a triage assistant for a
  LINE support group and answer only in Thai.
- Structured output is enforced via a JSON schema (`ISSUE_SCHEMA` passed as
  Ollama's `format` param) plus a restated key list in the final instruction,
  working around an Ollama schema-enforcement quirk.
- `think=False` disables the model's reasoning mode deliberately — a
  documented ~5s vs ~55s latency tradeoff.
- `warmup()` fires a throwaway call at process startup (both `api` and
  `issue-worker`) to preload the model and avoid a ~60s cold-start on the
  first real request.
- Images are **not** sent to a vision model — captures are already
  human-curated, so only the text transcript is summarized; attachments are
  linked onto the GitHub issue separately.
