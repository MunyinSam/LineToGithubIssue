# LineToGithubIssue v2: build-it-yourself guide

## How to use this guide

There are two parts:
- **Part A:** foundations, which fix v1.
- **Part B:** the harness: context, memory, feedback and evaluation.

Build them in the order given in the [Roadmap](#roadmap). Every chapter has the same structure:

- **Goal**: what exists when you're done.
- **Problem in v1**: the concrete thing that's wrong, with file pointers.
- **Solution / Why this one**: the choice, and the alternatives that lost.
- **Steps**: in order.
- **Sketch**: skeleton code or SQL. The bodies (`TODO`, `...`) are yours to write.
- **Pitfalls**.
- **Done when**: a check you can actually run.

Items marked **(verify)** are third-party API details I'm not fully sure of. Check the official docs before you build on them. That's a good habit for every external API anyway.

---

## The mental model

v1 is a straight pipeline: capture → prompt → issue. v2 is the same pipeline with a loop around it.

```
          ┌────────────── 1. CONTEXT ──────────────┐
capture ──► ContextBuilder (layers + token budget) ──► LLM ──► draft ──► GitHub issue
                ▲                                       │                    │
                │ 4. MEMORY                             ▼ 2. RUN LOG          ▼ 3. FEEDBACK
         similar issues, few-shot                   llm_runs           LINE buttons, `ai fix`,
         examples, group facts                                         GitHub edits, close reason
                ▲                                                            │
                └──────────── 5. EVAL: settled runs → eval_cases ◄───────────┘
                                 replay any prompt/model/context, score it
```

**Core idea:** you can't improve what you can't measure, and you can't measure what you didn't log. That's why the order is log → feedback → context → memory → eval. Data has to pile up before the later parts can tell you anything.

### Target folder layout

```
app/
  core/      config.py, db.py (async pool), migrate.py, logging.py
  line/      routes.py, dispatcher.py, commands.py, feedback.py, media.py
  llm/       client.py (interface), ollama.py, embeddings.py
  harness/   context_builder.py, budget.py, layers/*.py, draft.py, prompts.py, runlog.py
  memory/    issues.py, facts.py
  github/    client.py, publish.py, sync.py
  worker/    main.py (job loop), jobs.py
  eval/      __main__.py, scorers.py, judge.py
migrations/  001_init.sql, 002_queue.sql, ...
prompts/     issue_v1.md, judge_v1.md
tests/
```

**Why split it this way:** each folder has one reason to change. In v1, `app/services/summarizer.py` holds prompting, HTTP, JSON recovery and validation all at once. Changing a sentence of the prompt meant editing a file that also does network I/O.

---

# Part A: Foundations

## A0. Pin dependencies

**Problem in v1:** `requirements.txt` has no versions and lists `fastapi`, `uvicorn` and `aiohttp` twice. Any rebuild can pull a new major version of `line-bot-sdk` or `psycopg`, and there are no tests to notice.

**Solution:** use `uv` (`uv init`, `uv add fastapi ...`, which commits `uv.lock`). `pip-tools` (`requirements.in` → `pip-compile`) is fine too. Add dev dependencies: `pytest`, `pytest-asyncio`, `ruff`.

**Why:** a lockfile makes "works on my machine" and "works in the container" the same thing. `uv` handles the venv, the lock and the install in one fast tool.

**Done when:** a fresh venv or container build installs byte-identical versions.

---

## A1. Migrations runner

**Goal:** schema changes are numbered files. Each is applied exactly once, safely, even when two processes boot at the same moment.

**Problem in v1:**
- Both processes re-run `app/line/schema.sql` on every boot (`ensure_schema()` in `app/main.py` and in `app/workers/issue_worker.py`).
- Changes get bolted on as `DO $$ ... $$` blocks (the `issue_requests` → `issue_requests_anchor_v1` rename).
- `CREATE TABLE IF NOT EXISTS` is **not** safe to run concurrently in Postgres. Two sessions racing can fail with a unique violation on `pg_type`.

**Solution:** a small migration runner of about 40 lines:
- Migrations live in files named `migrations/NNN_name.sql`.
- A `schema_migrations(version, name, applied_at)` table records what has run.
- The runner takes `pg_advisory_lock(<constant>)` first, so concurrent boots wait for each other.
- Each unapplied file runs in **its own transaction** together with its insert into `schema_migrations`.

**Why this one:**
- You write raw SQL and use no ORM, so you want a tool that runs raw SQL files.
- Writing it yourself teaches two important Postgres features:
  - **Advisory locks**: app-defined locks on an arbitrary number.
  - **Transactional DDL**: Postgres can roll back a `CREATE TABLE`, which most databases can't. A half-applied migration is impossible.
- **Alternatives:**
  - Alembic is designed around SQLAlchemy, which is heavy if you have no ORM.
  - `dbmate` and `yoyo-migrations` are good and use the same model. Use one of them if you'd rather not write it.

**Steps:**
1. Write `migrations/001_init.sql` to match the **current prod schema exactly**, without the `DO $$` block.
2. On the existing prod DB, insert `version=1` by hand ("baselining") so 001 never runs there.
3. Write `app/core/migrate.py`.
4. Run it as its own step before the app starts, e.g. `command: sh -c "python -m app.core.migrate && uvicorn ..."`, or call it at startup in both processes. The lock makes either approach safe.

**Sketch:**

```python
# app/core/migrate.py
LOCK_ID = 7_461_001  # any constant bigint; every process must use the same one
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

async def migrate(conninfo: str) -> None:
    # autocommit=True so conn.transaction() opens a REAL transaction per file
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    int PRIMARY KEY,
                    name       text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now())""")
            cur = await conn.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in await cur.fetchall()}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                async with conn.transaction():
                    # TODO: execute path.read_text()
                    # TODO: INSERT INTO schema_migrations(version, name)
                    ...
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
```

**Pitfalls:**
- **Never edit an applied migration.** Add a new one instead. For extra safety, store a checksum per file and refuse to start if an applied file has changed.
- `CREATE INDEX CONCURRENTLY` can't run inside a transaction. Avoid it at your table sizes.
- A migration that adds a `NOT NULL` column to a table with rows needs a `DEFAULT`.

**Done when:** you start the API and the worker at the same moment against an empty DB, and you get one clean schema with each version in `schema_migrations` exactly once.

---

## A2. Async database layer

**Goal:** nothing blocks the API's event loop.

**Problem in v1:** `app/line/dispatcher.py` is `async`, but every `db.*` call goes to `app/line/controllers.py`, which uses the **sync** `psycopg_pool.ConnectionPool`. While one query runs, the whole FastAPI process is frozen: other webhooks, image downloads, everything. Low traffic hides this. A burst of forwarded photos exposes it.

**Solution:** `psycopg_pool.AsyncConnectionPool` with `async def` repository functions. Keep raw SQL; v1's move away from the ORM was a fine decision.

**Why this one:**
- psycopg 3 has native async: the same SQL and parameter style, with no threads involved.
- **Alternatives:**
  - `asyncio.to_thread(sync_call)` works, but every call costs a thread hop, and forgetting one wrapper silently brings the blocking back.
  - `asyncpg` is faster, but it uses `$1` parameters, so you'd rewrite every query.
  - SQLAlchemy async would bring the ORM back.

**Steps:**
1. In `app/core/db.py`, create `pool = AsyncConnectionPool(conninfo, min_size=..., max_size=..., open=False)`. Run `await pool.open()` in the FastAPI `lifespan` and in the worker's `main`.
2. Split `controllers.py` (550 lines) into repositories per table: `repo/messages.py`, `repo/captures.py`, `repo/requests.py`, `repo/profiles.py`.
3. Switch row models from dataclasses to Pydantic so `.model_dump()` gives SQL params directly. `class_row(Model)` still works.
4. Rename the `pgvector_*` settings to `db_*`.

**Sketch:**

```python
async def insert_message(msg: ContextMessage) -> int | None:
    async with pool.connection() as conn:
        cur = await conn.execute(INSERT_MESSAGE_SQL, msg.model_dump())
        row = await cur.fetchone()
        return row[0] if row else None  # None = duplicate (ON CONFLICT DO NOTHING)
```

**Pitfalls:**
- `async with pool.connection()` **commits on clean exit and rolls back on exception**. Know that this is what's happening.
- **Never hold a connection across a slow `await`** such as an HTTP call to LINE, Ollama or GitHub. Five slow calls holding five connections will drain a small pool. The pattern is: read → release → call the API → acquire again → write.
- Profile fetches and downloads in the webhook handler should be awaited properly or spawned (keep v1's `_background_tasks` set; that lesson still holds).

**Done when:** a grep shows no sync `pool.connection()` anywhere reachable from an `async def`.

---

## A3. Postgres as the job queue (replacing RabbitMQ)

**Goal:** the worker claims jobs directly from `issue_requests`. There's no broker, no re-publish sweep and no heartbeat workaround.

**Problem in v1:** four separate mechanisms exist only to keep a Postgres row and a RabbitMQ message in sync:
1. `heartbeat=0`, because a long job blocks pika's `BlockingConnection` thread.
2. `_requeue_stale_jobs()` runs in **both** `app/main.py` and `app/workers/issue_worker.py`. A job that has simply waited more than 10 minutes behind a slow one gets re-published on any restart and runs twice.
3. The `status == "done"` check against redelivery.
4. The cross-machine dependency on Machine B.

There's also a hole: `RECLAIM_STALE_SQL` only looks at `status='pending'`. A crash **after** summarizing leaves the row in `summarized` forever.

**Root cause:** there are two sources of truth, the row status and the broker message. Remove one.

**Solution: lease-based claiming in Postgres**

Add these columns to `issue_requests`:
- `status`: `queued | running | done | failed`
- `attempts`
- `locked_by`
- `locked_until`
- `run_after`

The claim is one short transaction:

```sql
UPDATE issue_requests
SET status = 'running',
    locked_by = %(worker_id)s,
    locked_until = now() + interval '15 minutes',
    attempts = attempts + 1
WHERE id = (
    SELECT id FROM issue_requests
    WHERE (status = 'queued'  AND run_after <= now())
       OR (status = 'running' AND locked_until < now())   -- a dead worker's lease expired
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

- **API, on `ai end`:** insert the row with `status='queued'`, then `NOTIFY issue_jobs` **in the same transaction**. NOTIFY is only delivered on commit, so the worker can never wake up before the row is visible.
- **Worker:** claim → nothing to do? → wait on `LISTEN issue_jobs` with a 30 s timeout → process → mark `done` or `failed`.

**Why this one:**
- **One source of truth.** The row *is* the job. If a worker crashes, its lease expires and the job is claimed again. No sweep code is needed, in either process.
- **`SKIP LOCKED`** means a second worker can never claim the same row. You could run two workers later without changing anything.
- **Why a lease instead of holding `FOR UPDATE` for the whole job:** a row lock held for minutes means a transaction open for minutes. That holds a connection and blocks vacuum. With a lease, the transaction is milliseconds long and the "lock" is a timestamp.
- **Scale:** you file a few jobs a day, and Postgres queues handle thousands per second.
- **Alternatives:**
  - Keep RabbitMQ with `aio-pika` (async, so heartbeats work). That's still two sources of truth.
  - Celery, RQ or arq add another broker (Redis).
  - `procrastinate` and `pgqueuer` are libraries that implement this exact pattern on Postgres. They're a good choice if you don't want to write it, and writing it once teaches you what they do inside.
- **Keep RabbitMQ** only for other consumers, such as the PVSD `line_notify` queue. That's separate from this pipeline.

**Retry policy:** v1 never retries, for a good reason: the same LLM input fails the same way again. But GitHub 502s and network blips are transient and deserve a retry. Split errors into two classes:
- `RetryableError` → `status='queued', run_after = now() + backoff(attempts)`, with a max of about 3 attempts.
- Everything else → `failed`, and notify the group (same as v1).

**Sketch:**

```python
async def run_worker(worker_id: str) -> None:
    listen = await AsyncConnection.connect(settings.db_conninfo, autocommit=True)
    await listen.execute("LISTEN issue_jobs")
    while not shutting_down.is_set():
        job = await repo.claim_next_job(worker_id)
        if job is None:
            # Sleep until a NOTIFY arrives, or 30 s pass (covers missed notifies).
            async for _ in listen.notifies(timeout=30):   # (verify) `timeout` needs psycopg >= 3.2
                break
            continue
        try:
            await process_job(job)
            await repo.mark_done(job.id)
        except RetryableError as e:
            await repo.schedule_retry(job.id, str(e))
        except Exception as e:
            await repo.mark_failed(job.id, str(e))
            await notify_failure(job)
```

**Pitfalls:**
- **NOTIFY is a wake-up hint, not a delivery guarantee.** A notify sent while the listener reconnects is lost. Always claim from the table; the timeout poll covers the gaps.
- **Lease length:** it must exceed the worst-case job (`ollama_timeout_s` × LLM calls + uploads). Alternatively, renew the lease from a background task every minute.
- **Graceful shutdown:** on SIGTERM, stop claiming, then finish the current job or let its lease expire. Don't mark it failed.
- **One Ollama means one effective worker.** A second worker would just queue on the GPU.

**Done when:** you `kill -9` the worker mid-job, restart it, and the job is claimed again after the lease expires and completes. A4 makes sure GitHub still only sees it once.

---

## A4. Exactly-once GitHub issue creation

**Goal:** any crash or retry still produces **at most one** GitHub issue per capture.

**Problem in v1:** GitHub's create-issue API has no idempotency key. If the process dies after GitHub created the issue but before `db.mark_issue_done()` saved it, the retry creates a duplicate. Also, if `mark_issue_done` itself throws (a DB blip), the catch-all in `on_message` marks the request `failed` and tells the group it failed, even though the issue exists.

**Solution: record intent, add a marker, reconcile.**
1. **Before** calling GitHub, save `publish_state='creating'` and `publish_started_at=now()`.
2. Put an invisible marker in the body: `<!-- ltgi:req=123 -->`. HTML comments don't render in GitHub markdown.
3. After success, save the number and URL, and set `publish_state='created'`.
4. On retry with `publish_state='creating'`:
   - List the issues the bot created since then: `GET /repos/{o}/{r}/issues?creator=<bot>&since=<publish_started_at>&state=all`.
   - Scan their bodies for the marker.
   - If you find it, adopt that issue. If not, it's safe to create.

**Why this one:**
- You can't make "call GitHub" and "write to Postgres" atomic, because there's no transaction spanning both systems. So you **record intent first and reconcile after**. This is the general pattern for any non-idempotent external API (payments, emails, issue trackers), so it's worth understanding deeply.
- **Why list-and-scan rather than the search API:** GitHub search is eventually consistent (an indexing delay), and whether HTML comments get indexed is doubtful **(verify)**. A just-created duplicate might not show up in search yet. Listing by creator and `since` reads the source of truth.

**Pitfalls:**
- `since` filters by *updated* time, not created time. That's fine, since anything newly created was also recently updated.
- This works cleanly only if **one account** creates every issue. That's one more reason for A5.
- Keep v1's LINE push dedupe (`X-Line-Retry-Key` = uuid5 of a stable key). It's the same idea applied to LINE.

**Done when:** a test that raises an exception right after `create_issue` returns, then re-runs the job, ends with exactly one issue and a `done` row.

---

## A5. Identity: link LINE users by `user_id`, not display name

**Problem in v1 (security):** `settings.github_token_by_name` maps **LINE display name → GitHub token**, and `issue_worker.py` picks the token with `.get(reporter)`. Users choose their own LINE display names, and names aren't unique. Anyone in the group can rename themselves to match a configured name and file issues as that person's GitHub account.

**Solution:**
- Add a table `user_links(line_user_id text PRIMARY KEY, github_login text NOT NULL, linked_by text, created_at)`.
- **File every issue with one bot identity:** a fine-grained PAT on a bot account, or a GitHub App. Credit the reporter in the body ("Reported by @login via LINE") and/or with `assignees: [login]`.
- **Linking flow:** `ai link <github_login>` creates a *pending* link that an admin approves. Otherwise someone could claim another person's login.

**Why:**
- LINE `user_id` is stable and can't be forged within your channel.
- One bot token instead of N personal tokens means there's nothing per-user to rotate, leak or expire, and A4's "list by creator" works.
- **Trade-off:** the issue's *creator* is the bot, not the person. The mention or assignment still gives them notifications and attribution.
- **Alternative:** if "the creator must be the actual person" is a hard requirement, keep per-user tokens. Key them by `user_id` and keep them in a secrets file, not the DB or a comma-separated env var.

**Done when:** renaming yourself in LINE changes nothing about which GitHub identity files or credits the issue.

---

## A6. LLM client interface and typed drafts

**Goal:** (a) the harness doesn't know or care that it's talking to Ollama. (b) Model output is validated in exactly one place.

**Problem in v1:** `app/services/summarizer.py` mixes HTTP, prompt assembly, JSON recovery and type coercion (`_coerce_str_list`, the `float()` try, the missing-keys check). `IssueDraft` is a plain dataclass, so wrong types leaked downstream. v1 already hit two of these: steps exploding into one step per character, and `:.2f` crashing on a string confidence.

**Solution:**

```python
# app/llm/client.py
@dataclass
class LLMResult:
    content: str
    done_reason: str | None
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    model: str

class LLMClient(Protocol):
    async def chat(self, messages: list[dict], *, schema: dict | None = None,
                   images: list[bytes] | None = None) -> LLMResult: ...
```

- `OllamaClient` implements it. Fill the token counts from `/api/chat`'s `prompt_eval_count` and `eval_count`, and the latency from `total_duration` (nanoseconds) **(verify against your Ollama version)**.
- Make `IssueDraft` a Pydantic model:
  - `severity: Literal["low", "medium", "high", "critical"]`
  - `confidence: float = Field(0, ge=0, le=1)`
  - `steps_to_reproduce` and `labels` get a `@field_validator(..., mode="before")` that does what `_coerce_str_list` does today.
  - Use `IssueDraft.model_json_schema()` as Ollama's `format`. That gives one definition and no drift between the schema you send and the validator you apply.
- **Keep v1's `_parse_json`** (fence stripping, `strict=False`, brace slicing). It's battle-tested; move it to `app/harness/draft.py`.

**Why:**
- The interface is what lets the eval harness (B12) compare `qwen3.5:9b` against another model, or use a bigger *judge* model, with a flag instead of a code change.
- With Pydantic, "what a valid draft is" becomes one readable class.

**Pitfalls (keep v1's hard-won lessons, and re-write the comments next to the new code):**
- `think=False`: thinking tokens starved `num_predict` and caused random truncation.
- Spell out the JSON keys in the final instruction. On Ollama 0.30.10, `format` stopped being enforced when `think=false`.
- Keep `num_ctx` pinned. Changing it forces a model reload of about 60 s.
- Keep the startup `warmup()` call.

**Done when:** unit tests feed in every bad output v1 has seen and get either a valid `IssueDraft` or a clear `DraftInvalid` error. Those outputs are: fenced JSON, prose around the object, a string `confidence`, a string `steps_to_reproduce`, a raw newline inside a string, and missing keys.

---

## A7. Prompts as versioned files

**Goal:** every run records exactly which prompt produced it.

**Problem in v1:** `SYSTEM_PROMPT` and `FINAL_INSTRUCTION` are string constants. Once you change one, old and new issues can't be told apart, so you can never say whether accuracy changed *because of* the prompt.

**Solution:**
- Keep `prompts/issue_v1.md` with front-matter (`version`, `description`) and a template body. Use Jinja2, or `string.Template` to stay dependency-light.
- Set `prompt_id = f"issue_v1@{sha256(file_bytes)[:8]}"`. The hash catches "I edited v1 in place and forgot to bump it."
- Choose the active prompt by config (`ISSUE_PROMPT=issue_v1`). The eval CLI can load any version.

**Why files:**
- Prompt changes become readable git diffs.
- Your help-desk teammates can propose wording changes without touching Python.
- Eval can load any version side by side.

**Why the hash:** people forget to bump version numbers, and a hash can't forget.

**Done when:** every `llm_runs.prompt_id` (B1) is filled in.

---

## A8. Attachments

### A8a. Show images to the model

**Problem:** in a help-desk capture, the screenshot of the error dialog is often *the* key information. v1 sends the model only `[image attached]`.

**Steps:**
1. Run `ollama show qwen3.5:9b`. If the capabilities list `vision`, send base64 images in the message's `images` field **(verify the field for your Ollama version)**.
   - Downscale first, e.g. 1024 px on the longest side. `app/line/media.py` already generates `.preview` files, so reuse them.
   - Cap the count (e.g. 4 images), because images are expensive in context.
2. If the model isn't vision-capable, OCR the image (Tesseract with the Thai `tha` language data, or a small vision model) and add `[image text: ...]` to the transcript.
3. **Decide with data:** run the B12 eval with and without images. This is exactly the kind of question the harness exists to answer.

### A8b. The public attachment route

`/line/attachments/{path}` is protected only by an unguessable path. The path includes the conversation id, which isn't public, so the risk is moderate to low.

**Cheap hardening:** append `?sig=<HMAC(secret, path)>` to the URL and verify it on the route.

**Don't add an expiry.** GitHub serves issue images through its *camo* proxy, which re-fetches the image when its cache expires. An expiring URL would break images in old issues.

**Also decide:** today you upload to GitHub release assets *and* serve from your own server.
- Release assets are durable: they survive your server going away.
- Self-hosted files give inline previews.
- A reasonable split is images inline from your server and documents (PDF, xlsx) as release assets. This is low priority, since v1 works.

---

## A9. Tests

**Goal:** a test suite that runs in seconds for the logic, plus a few tests against a real Postgres.

**What to test first, in order of value per line of test:**
1. `parse_command`, as a table-driven test. `"ai end of the month"` must **not** match; `"@Bot ai end"` must. Add the new `ai note`, `ai fix` and `ai link` forms.
2. Draft parsing: every case listed in A6.
3. The context builder's budget and ordering (B2). It's a pure function of its inputs.
4. `render_issue_body`.
5. Queue claim and lease expiry (A3), against real Postgres.
6. A crash right after creating the issue (A4).

**Why a real Postgres for DB tests:** your important logic *is* SQL: `SKIP LOCKED`, the partial unique index `ux_capture_open_per_conv`, `ON CONFLICT`. A mock can't test SQL semantics. Use a throwaway DB in Docker, or `pytest-postgresql`.

**Done when:** `pytest` runs in under 30 s locally, and a GitHub Actions workflow runs it on every push.

---

# Part B: The harness

## B1. Run log (`llm_runs`)

**Goal:** every LLM call is recorded completely enough to **replay** it and **diagnose** it.

**Why first:** every later chapter consumes this table. Feedback (B5, B6) attaches to runs, eval (B12) replays them and metrics (B13) aggregate them. Start logging *now* so data accumulates while you build the rest.

**Schema:**

```sql
CREATE TABLE llm_runs (
  id               bigserial PRIMARY KEY,
  issue_request_id bigint REFERENCES issue_requests(id),
  purpose          text NOT NULL,        -- 'issue' | 'judge' | 'fact_extract' | 'eval'
  prompt_id        text NOT NULL,        -- 'issue_v1@3fa2c1d0'
  model            text NOT NULL,
  params           jsonb NOT NULL,       -- temperature, num_ctx, num_predict, think
  context_manifest jsonb NOT NULL,       -- what went in, per layer (ids, counts, drops)
  messages         jsonb NOT NULL,       -- the exact messages[] sent
  raw_output       text,
  draft            jsonb,                -- parsed IssueDraft; NULL if invalid
  done_reason      text,
  prompt_tokens    int,
  output_tokens    int,
  latency_ms       int,
  error            text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
```

Example `context_manifest`:

```json
{"capture": [101, 102, 103], "quoted": [88], "preroll": [95, 96],
 "similar_issues": [12, 40], "facts": [3], "dropped": {"preroll": 12}, "est_tokens": 5400}
```

**Why both `messages` and `context_manifest`:**
- `messages`, the exact text, gives a faithful replay even if the source rows change later (a message gets unsent, a profile name changes).
- `context_manifest`, the ids, is for analysis, e.g. "do runs that included pre-roll get more 👍?"

**Pitfalls:**
- **Privacy:** this stores chat verbatim. Decide on retention, e.g. null out `messages` after 180 days and keep the numbers. Also decide who can read this DB.
- **Log failures too:** invalid JSON, truncation, timeouts. Failures make the most valuable eval cases.

**Done when:** every job writes at least one row, including failed jobs, and you can paste a row's `messages` into a script and reproduce the output.

---

## B2. ContextBuilder and token budget

**Goal:** one component turns "a capture" into "the best prompt that fits", pulling from several sources, and records what it did.

**Problem in v1:**
- The context is the capture only, trimmed by message *count*.
- The two trimming rules contradict each other:
  - `get_capture_messages` keeps the **oldest** 200, because the problem is stated first.
  - The truncation retry in `issue_worker.py` keeps the **newest** half (`msgs[-n:]`).

**Design:**

```python
@dataclass
class ContextBlock:
    layer: str           # 'capture' | 'quoted' | 'preroll' | 'similar' | 'facts' | 'repo'
    priority: int        # lower = more important = dropped last
    header: str          # the label the model sees
    text: str
    source_ids: list     # goes into the manifest
    est_tokens: int

class Layer(Protocol):
    name: str
    async def build(self, req: IssueRequest, budget_tokens: int) -> ContextBlock | None: ...
```

**Builder algorithm:**
1. **Fixed costs first:** `available = num_ctx − system prompt − final instruction − output reserve (num_predict) − safety margin`.
2. **Capture gets first claim**, because it's what the human chose. Every other layer has a cap, for example:

   | Layer | Cap |
   |---|---|
   | quoted | 1.5k tokens |
   | preroll | 2k tokens |
   | similar | 3k tokens |
   | facts | 1k tokens |
   | repo | 1k tokens |

3. Each layer builds itself within its cap.
4. Render the blocks in a fixed order with **clear headers**:

   ```
   ### PRIMARY: messages the reporter selected (ai start → ai end)
   ...
   ### BACKGROUND: chat shortly before the recording (may be unrelated)
   ...
   ### REFERENCE: similar past issues (do not copy facts; flag if duplicate)
   ...
   ### GLOSSARY: facts about this group's systems
   ...
   ```

5. If the total is still over budget, drop layers in reverse priority order and record the drops in the manifest.

**Capture trimming policy** (fixes the v1 contradiction): if the capture alone is over budget, keep the **head** and the **tail**, and replace the middle with `[… N messages omitted …]`.
- The first messages state the problem.
- The last messages carry the latest status.
- The middle of a long forwarded chain is most often repetition.

**Token counting:** Ollama has no tokenizer endpoint you can rely on **(verify)**. There are two options:
- **Heuristic:** `tokens ≈ chars / k`. Calibrate k from your own `llm_runs` data (`prompt_tokens` vs. character count). **Thai tokenizes very differently from English, so calibrate rather than guess.**
- **Exact:** load Qwen's tokenizer with Hugging Face's `tokenizers` library.

Start with the heuristic plus a 15% margin. The run log will tell you when it's off.

**Truncation retry, redone:** `done_reason == "length"` means the **output** ran out, not that the input was too big. Instead of halving the capture like v1, retry with the optional layers dropped and a "be concise" hint. If it still truncates, fail.

**Why layered and labelled:**
- The model needs to know **how much to trust** each piece. Unlabelled pre-roll would make it report the wrong problem; labelled as BACKGROUND, it's just context.
- **Why per-layer caps:** one greedy layer (five long similar issues) must never crowd out the capture.

**Done when:** a unit test with a 300-message capture and all layers enabled produces a prompt under budget, with the capture's head and tail kept and a manifest listing what was dropped.

---

## B3. Layer: quoted originals

**Goal:** when a captured message is a LINE *reply*, the model sees what it replied to.

**Why:** "same here, still broken" is meaningless without the quoted message. v1 already stores `quoted_message_id` but never uses it. This is the cheapest context win available.

**Steps:**
1. For capture messages whose `quoted_message_id` isn't itself in the capture, run `SELECT ... FROM context_messages WHERE line_message_id = ANY(%s)`.
2. Render it **inline**, right where it's used:

   ```
   [10:42] Nok (replying to [09:15] Ton: "error 504 on export"): same here
   ```

   **Why inline rather than a separate block:** the reply relationship *is* the information.
3. If the original isn't in the DB (for example, sent before the bot joined), render `(replying to a message not available)`. That tells the model something is missing instead of silently hiding it.

**Pitfall:** reply chains. Resolve **one level only**; chasing recursively can explode the context.

**Done when:** a capture containing a reply shows the quoted text in `llm_runs.messages`.

---

## B4. Layer: pre-roll (chat before `ai start`)

**Goal:** include the conversation that led up to the capture, without dragging in unrelated chat.

**Why:** people often describe the problem first and only *then* think "let me log this". Those messages never reach v1's model. They're already stored in `context_messages`, so this layer is just a query.

**Solution: time-gap segmentation**
- Walk backwards from the `ai start` message, collecting messages while the gap between consecutive messages is under **G** (e.g. 10 min).
- Stop at **N** messages (e.g. 30) or **T** minutes (e.g. 60).
- Also **stop at the end of any previous capture session**, so an issue that was already logged isn't summarized again.
- Exclude bot messages and `ai ...` commands.

**Why time gaps instead of a fixed "last N messages":** chat happens in bursts. A fixed "last 20" might be 20 messages about lunch from three hours ago. A gap of more than 10 minutes is a decent proxy for a topic boundary, and it needs no ML.

**Alternative (later):** ask the LLM which pre-roll messages are related. That costs a second call, so only do it if eval shows pre-roll is hurting.

**Prompt rule:** "Use BACKGROUND only to clarify the PRIMARY messages. Never report a problem that appears only in BACKGROUND."

**Done when:** the manifest lists pre-roll ids, and you've recorded one eval run with pre-roll on and one with it off (B12).

---

## B5. Feedback from LINE: buttons and `ai fix`

**Goal:** reporters can rate an issue in one tap and correct it in one message.

**Solution:**
- Send the "Logged: <url>" result as a **Flex message (or buttons template) with postback buttons**: 👍 Good · ❌ Wrong problem · ➕ Missing info · ⚠️ Wrong severity. Use data like `data="fb:123:good"`.
- Handle `PostbackEvent` in the dispatcher: parse the data, insert into `feedback_events`, and reply "Thanks" using the reply token (replies are free).
- **`ai fix <text>`** is sent as a LINE *reply* to the bot's result message:
  - Its `quoted_message_id` identifies which request it's about.
  - For that to work, **store the sent message id** returned by the push API (`sentMessages[].id`) **(verify)** against the request.
  - Optionally, also post the correction as a GitHub comment on the issue.

**Why message buttons and not quick replies:** LINE quick-reply buttons disappear as soon as anyone sends another message, which in a group chat is seconds. Buttons inside a Flex or template message stay attached to that message.

**Why postback:** the tap reaches your webhook as structured data without posting text into the group.

**Quota:** put the buttons in **the same push request** as the result; one request can carry up to 5 message objects. Don't send a separate "please rate" push. Push quota for groups is counted per recipient **(verify for your LINE plan)**, so extra pushes to a big group are expensive.

**Schema:**

```sql
CREATE TABLE feedback_events (
  id               bigserial PRIMARY KEY,
  issue_request_id bigint NOT NULL REFERENCES issue_requests(id),
  source           text NOT NULL,  -- 'line_button' | 'line_fix' | 'github_sync'
  kind             text NOT NULL,  -- 'good' | 'wrong_problem' | 'missing_info' | 'wrong_severity'
                                   -- | 'correction' | 'title_changed' | 'body_changed' | 'closed' ...
  actor            text,           -- LINE user_id or GitHub login
  payload          jsonb,          -- free text, similarity score, close reason, label diff...
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issue_request_id, source, kind, actor)   -- one vote per person per kind
);
```

**Pitfalls:**
- Decide who may vote: anyone in the group, or only the reporter? Always store `actor`, so the metrics can decide later.
- Explicit feedback is **sparse and biased**, because people mostly rate when they're annoyed. That's why B6 exists.

**Done when:** tapping a button creates a row, and tapping it twice doesn't create two.

---

## B6. Feedback from GitHub (the strongest signal)

**Goal:** capture what humans actually changed (final title, body, labels, severity, close reason) as **ground truth**.

**Why it's the best signal:**
- It's free; nobody has to rate anything.
- It reflects what developers actually needed.
- The final edited issue is literally the "correct answer" for eval.

**Solution: a periodic sync job** (not webhooks, at first):
- Daily or hourly, for issues filed in the last 30 days, call `GET /repos/{o}/{r}/issues/{n}`.
- If anything differs from the last snapshot, store a new row in `issue_snapshots(issue_request_id, title, body, labels, state, state_reason, comments, fetched_at)`.
- Derive `feedback_events`:
  - `title_changed`
  - `body_changed`, with a similarity score between the draft and the final body
  - labels added or removed
  - severity changed
  - closed with `completed`, `not_planned` or `duplicate` **(verify which `state_reason` values your GitHub returns)**
- When an issue is closed or more than 14 days old, mark its last snapshot **settled**. Settled issues feed eval (B12) and memory (B7).

**Why polling over webhooks:**
- You need the **final** state, not every edit event.
- Polling needs no public endpoint, no signature verification and no handling of missed deliveries, and your volume is tiny.
- Move to webhooks (`issues` events, verified with `X-Hub-Signature-256`) only if you want real-time reactions.

**Pitfalls:**
- **Strip your own generated parts** (the footer, the "Reported by" line, the marker, the attachments list) before comparing draft and final. Otherwise every issue looks "edited".
- Ignore the auto labels (`AI`, `needs-triage`) in label accuracy.
- Use conditional requests (`If-None-Match` with the ETag). A `304` doesn't count against the rate limit.

**Done when:** you edit an issue's title on GitHub, and a `title_changed` event appears after the next sync.

---

## B7. Embeddings and issue memory (pgvector)

**Goal:** a searchable memory of past issues, stored in their **human-corrected** form.

**Solution:**
- Your Postgres is already the pgvector image (`.env.example` calls it `pgvector-db`), so run `CREATE EXTENSION IF NOT EXISTS vector;`.
- `qwen3-embedding:4b` is already in `app/core/config.py` and unused. Call Ollama's `POST /api/embed` with `{"model": ..., "input": [texts]}` to get back `embeddings` **(verify)**. Check the vector dimension once with `len(vec)`.

```sql
CREATE TABLE issue_memory (
  id               bigserial PRIMARY KEY,
  issue_request_id bigint UNIQUE REFERENCES issue_requests(id),
  conversation_id  text NOT NULL,
  github_number    int,
  title            text NOT NULL,     -- FINAL title once settled
  summary          text NOT NULL,     -- short: the problem (+ resolution if known)
  capture_excerpt  text,              -- short input excerpt, used for few-shot (B9)
  final_draft      jsonb,             -- final title/body/labels/severity
  labels           text[],
  state            text,
  state_reason     text,
  quality          text,              -- 'good' | 'corrected' | 'bad' | NULL (unknown yet)
  embedding        vector(2560) NOT NULL,   -- use YOUR model's dimension
  embed_model      text NOT NULL,
  updated_at       timestamptz NOT NULL DEFAULT now()
);
```

**Lifecycle:**
- **Insert** when the issue is filed. At that point it holds the draft version and `quality = NULL`.
- **Update** when it settles (B6). It then holds the final version, its quality, and a fresh embedding.

**Query:** `ORDER BY embedding <=> %(q)s LIMIT 5` (`<=>` is cosine distance), filtered to the same `conversation_id` or repo.
- **No index needed** until you have tens of thousands of rows. An exact scan is fast and exact.
- Add an HNSW index (`USING hnsw (embedding vector_cosine_ops)`) later.

**What to embed:** store embeddings of *issues* (final title + summary), and query with an embedding of the *new capture text*. Both describe "the problem", so they land near each other. B12 can measure whether this works ("did the true duplicate show up in the top 3?").

**Why pgvector over a vector DB** (Qdrant, Chroma, Pinecone):
- It's already in your Postgres.
- It can JOIN with your other tables.
- It's covered by the same backup and the same transactions.
- A separate vector DB only pays off at a scale you won't reach.

**Pitfalls:**
- **VRAM:** the chat model (9B with 32k context) and the embedding model (4B) may not fit in GPU memory together. If they don't, Ollama evicts one and you pay the ~60 s reload that v1 works hard to avoid. Check with `ollama ps`. Options:
  - a smaller embedder (`qwen3-embedding:0.6b`)
  - `OLLAMA_MAX_LOADED_MODELS=2` if VRAM allows
  - embedding in a batch outside busy hours
- **Store `embed_model`.** Vectors from different models aren't comparable, so a model change means re-embedding everything.

**Done when:** `python -m app.memory.search "export ค้าง"` prints relevant past issues.

---

## B8. Layer: similar past issues and duplicate hints

**Goal:** the model sees the 2–3 most similar past issues, and likely duplicates are flagged to humans.

**Steps:**
1. Embed the PRIMARY capture text, fetch the top k from `issue_memory`, and filter by a distance threshold.
2. Render the results as a REFERENCE block:

   ```
   #123 (closed: completed) "Export to Excel times out": <summary>
   ```

3. Add `possible_duplicate_of: int | None` to `IssueDraft`. The prompt says: "If a REFERENCE issue describes the same problem, set possible_duplicate_of."
4. If it's set and that issue is still open, add a line to the body ("Possibly a duplicate of #123") and to the LINE reply. **Don't auto-close; humans decide.**

**Why the model judges duplicates, not just a distance threshold:** similar isn't the same problem. "Export times out" and "export has the wrong format" are close vectors. The split is: retrieval *proposes*, the LLM *decides*, a human *confirms*.

**Choosing the threshold from data:** compute distances for issue pairs you know are duplicates (closed as `duplicate`) and for pairs you know aren't, and look at where the two distributions separate.

**Pitfall: contamination.** The model may copy details from the reference into the new issue. The prompt says not to, and the B12 grounding scorer catches it when it happens anyway.

**Done when:** a capture of a known recurring problem produces a draft with `possible_duplicate_of` set.

---

## B9. Dynamic few-shot (the "remembering" part)

**Goal:** the model learns your team's preferred issue style from approved past examples, without any fine-tuning.

**Solution:**
- From `issue_memory WHERE quality IN ('good', 'corrected')`, pick the **1–2 most similar** to the current capture.
- Insert each as an example pair: input (`capture_excerpt`) → output (`final_draft`, the human-approved JSON).

**Why few-shot instead of fine-tuning:**
- There's no training pipeline and no GPU training.
- It's reversible instantly.
- It improves **the moment someone corrects an issue**.
- Fine-tuning a 9B model needs hundreds of quality examples plus a training setup. Revisit it only if eval plateaus and you have 500+ settled cases.

**Why *similar* examples rather than fixed ones:** a printer-problem example helps a printer problem more than a login example does, and the style the team wants for each kind of problem emerges by itself.

**Pitfalls:**
- **Context cost:** keep excerpts short, use at most 2 examples, and give them lower priority than the capture.
- **Copying:** prefer examples from other conversations or older dates. Label them clearly as `EXAMPLE (different problem)`.
- **Measure it:** run eval with 0, 1 and 2 shots. Few-shot often helps small models with *format* but can hurt *grounding*, so let the numbers pick k.

**Done when:** eval shows the score with few-shot is at least as good as without, and k was chosen from the data.

---

## B10. Group facts (`ai note`)

**Goal:** a curated per-group glossary of system names, product codes, owners and jargon. For example: "ระบบ X = the billing service", or "PO-xxxx are purchase orders in SAP".

**Solution:**
- Commands:
  - `ai note <fact>` → inserts into `group_facts(id, conversation_id, text, created_by, status, created_at)`
  - `ai notes` → lists the facts
  - `ai forget <id>` → removes one
- **Parser:** keep v1's philosophy that the *whole message* must be the command. For example: `^ai[\s,]+note[\s:]+(.+)$` (it requires text after "note").
- **Layer:** include all active facts for the group, which is usually a short list. If it grows, retrieve the top k by embedding.
- **Later:** have an LLM extract *candidate* facts from corrections (`ai fix` texts, GitHub edits) with `status='proposed'`, and show them to an admin with approve/reject buttons. **Never auto-activate a fact.** A wrong fact silently poisons every future issue for that group.

**Why human-curated first:** a few high-quality facts beat many noisy ones, and it takes about an hour to build.

**Done when:** after `ai note ระบบ A คือ billing`, the next draft for that group uses the right name.

---

## B11. Repo context layer

**Goal:** the model picks only labels that exist and knows what's currently open.

**Steps:**
1. Cache `GET /repos/{o}/{r}/labels` (name + description) once a day and include it in the prompt.
2. After parsing, drop unknown labels, matching case-insensitively.
3. Optionally, include the titles of the last 50 open issues. That's a cheap duplicate signal before B7 and B8 exist.

**Why:** invented labels clutter the repo and make label metrics meaningless.

**Done when:** every label on AI-filed issues already exists in the repo.

---

## B12. Evaluation harness

**Goal:** one command tells you whether a change to the prompt, model or context makes issues better or worse.

### Eval cases

```sql
CREATE TABLE eval_cases (
  id               bigserial PRIMARY KEY,
  issue_request_id bigint UNIQUE REFERENCES issue_requests(id),
  inputs           jsonb NOT NULL,  -- raw capture + layer SOURCE data at the time
  expected         jsonb NOT NULL,  -- final human version: title, body, labels, severity
  tags             text[],          -- 'thai', 'images', 'duplicate', 'long', 'failure'...
  created_at       timestamptz NOT NULL DEFAULT now()
);
```

- **Promote** a run to a case when it settles (B6) and was either edited by humans (expected = the final version) or rated 👍 without edits (expected = the draft).
- **Add hard cases by hand,** especially past failures.
- **Freeze the inputs, not the rendered prompt.** Store the raw messages and layer sources so you can evaluate *new context builders* on old cases, not only new prompts.

### Scorers (`app/eval/scorers.py`)

Each scorer returns a value from 0 to 1 plus details.

| Scorer | What it checks |
|---|---|
| `valid` | Output parsed into `IssueDraft`. |
| `severity_match` | Exact match with the expected severity. |
| `labels_f1` | Labels vs. expected, ignoring the auto labels. |
| `grounding` | Hard facts in the draft also appear in the inputs. See below. |
| `title_sim`, `body_sim` | Similarity to the expected text. See below. |
| `judge` (optional) | An LLM scores the draft against a rubric. See below. |

- **`grounding`:**
  - Extract the hard facts from the draft: numbers, error codes (`[A-Z0-9_.-]{3,}`), quoted strings and URLs.
  - Each one must appear in the inputs.
  - Score = the fraction found, and list the violations.
  - This catches hallucination cheaply and deterministically. **It's the scorer to trust most.**
- **`title_sim`, `body_sim`:** use **chrF** (character n-gram F-score, e.g. `sacrebleu`) or embedding cosine.
  - **Why not BLEU or ROUGE:** they're word-based, and Thai has no spaces between words. Without a Thai word segmenter, word metrics are meaningless. Character n-grams work for any script.
- **`judge`:** an LLM scores against a rubric in `prompts/judge_v1.md`: Same problem? Key facts missing? Facts invented? Actionable for a developer?
  - Prefer **pairwise** judging ("which of A and B is closer to the expected?"), run with **positions swapped**, and average. LLM judges have position bias, and they're better at comparing than at absolute scores.
  - A 9B model judging its own output is biased. If you can use a stronger model *for offline eval only*, judge with it. That's where the A6 interface pays off.
  - **Calibrate the judge:** compare it with your own judgment on about 20 cases before you trust it.

### CLI

```bash
python -m app.eval run --prompt issue_v2 --model qwen3.5:9b \
    --layers capture,quoted,preroll --tag thai --repeat 3
python -m app.eval compare results/2026-11-10_a.json results/2026-11-12_b.json
```

Output: the mean per scorer, a breakdown per tag, and the **10 worst cases with diffs**. Save the results as JSON (and/or to an `eval_results` table).

### Rules
- **Decide your main score up front** and write the weights down, e.g. `0.4·judge + 0.3·body_sim + 0.2·grounding + 0.1·labels_f1`.
- **A change ships only if** it doesn't regress `grounding` and doesn't lose on the main score.
- **Repeat each config 3×.** Temperature 0.2 still varies, and small differences on 30 cases are noise.
- Keep `num_ctx` identical across runs.

**Why build this at all:** without it, every prompt tweak "looks better on the one example I tried". With 30–100 real cases, you see that the tweak fixed 3 cases and broke 5.

**Done when:** you can answer "does pre-roll help?" with a number.

---

## B13. Metrics and weekly digest

**Goal:** see production accuracy over time without opening the DB.

**Metrics** (SQL views over `llm_runs`, `feedback_events` and `issue_snapshots`):

| Metric | Definition |
|---|---|
| Volume | Issues filed, and failures with their reasons. |
| 👍 rate | Good votes ÷ all button votes. |
| Edit rate | % of issues whose title or body changed, plus the median draft→final body similarity. |
| Severity accuracy, label F1 | Measured on settled issues. |
| Duplicate rate | % closed as duplicate. Lower means dup detection is working. |
| `not_planned` rate | Roughly "this wasn't a real issue". |
| Latency | p50 and p95, per `prompt_id`. |

**Break everything down by `prompt_id`.** Every prompt change then becomes a natural before/after experiment.

**Delivery:** a weekly job, either inside the worker loop or cron running `python -m app.jobs.digest`, pushes a short summary to an **admin** LINE chat. Not the main group, because of noise and quota. Later, you can point Metabase or Grafana at the views.

**Done when:** the Monday digest arrives with last week's numbers.

---

# Roadmap

| Milestone | Chapters | Outcome |
|---|---|---|
| **M1 Foundations** | A0, A1, A2, A6, A7, A9 | Same behaviour as v1, with a clean core and tests |
| **M2 Reliable pipeline** | A3, A4, A5 | No RabbitMQ, exactly-once issues, no impersonation |
| **M3 Observe** | B1, B5, B6 | Every run logged and feedback flowing. **Start collecting data as early as possible.** |
| **M4 Better context** | B2, B3, B4, B11, A8a | Wider context, measured through the manifest |
| **M5 Memory** | B7, B8, B9, B10 | Similar issues, duplicate hints, few-shot, facts |
| **M6 Measure** | B12, B13 | Eval CLI and digest; every later change is gated by eval |

**Why M3 comes before M4:** when you start adding context layers, you want weeks of baseline feedback to compare against.

**Cutover plan:**
1. Run v2 in a **test LINE group** against a **scratch GitHub repo**.
2. **Shadow mode:** v2 drafts silently for the real group (it logs to `llm_runs` but doesn't file issues). Compare its drafts with v1's real issues using the eval CLI.
3. Switch over once v2 scores at least as well.

# Decisions only you can make

- Drop RabbitMQ entirely? Only if nothing else uses it (A3).
- File as a bot and credit the reporter, or keep per-user tokens keyed by `user_id`? (A5)
- Who can vote and add notes: anyone in the group, or only the reporter and admins? (B5, B10)
- How long to keep chat text in `llm_runs.messages`? (B1)
- Vision model or OCR for images? Decide with eval (A8a).
