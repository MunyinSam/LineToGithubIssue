-- Idempotent schema for the LINE -> DB -> AI -> GitHub pipeline.
-- Applied at startup by app.core.db.ensure_schema(). No vector extension
-- needed: this flow does no embedding/semantic search.

CREATE TABLE IF NOT EXISTS webhook_events (
    webhook_event_id TEXT PRIMARY KEY,
    event_type       TEXT        NOT NULL,
    is_redelivery    BOOLEAN     NOT NULL DEFAULT FALSE,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS context_messages (
    id                      BIGSERIAL PRIMARY KEY,
    line_message_id         TEXT        NOT NULL UNIQUE,
    webhook_event_id        TEXT,
    conversation_type       TEXT        NOT NULL,
    conversation_id         TEXT        NOT NULL,
    sender_id               TEXT,
    sender_name             TEXT,
    message_type            TEXT        NOT NULL,
    text                    TEXT,
    quoted_message_id       TEXT,
    quote_token             TEXT,
    attachment_path         TEXT,
    attachment_preview_path TEXT,
    attachment_mime         TEXT,
    attachment_name         TEXT,
    attachment_bytes        BIGINT,
    attachment_status       TEXT        NOT NULL DEFAULT 'none',
    is_unsent               BOOLEAN     NOT NULL DEFAULT FALSE,
    line_ts                 TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ctx_conv_ts
    ON context_messages (conversation_id, line_ts, id);

CREATE INDEX IF NOT EXISTS ix_ctx_quoted
    ON context_messages (conversation_id, quoted_message_id)
    WHERE quoted_message_id IS NOT NULL;

-- A capture session is the span a human brackets by hand: "ai start" opens
-- one, "ai end" closes it, and everything sent in between is the issue. The
-- markers are ordinary context_messages rows, so the span is expressed as
-- their (line_ts, id) bounds rather than copied message text.
CREATE TABLE IF NOT EXISTS capture_sessions (
    id                BIGSERIAL PRIMARY KEY,
    conversation_id   TEXT        NOT NULL,
    conversation_type TEXT        NOT NULL,
    started_by        TEXT,
    start_message_id  TEXT        NOT NULL UNIQUE,
    start_ts          TIMESTAMPTZ NOT NULL,
    end_message_id    TEXT,
    end_ts            TIMESTAMPTZ,
    -- open|closed|cancelled|expired|empty
    status            TEXT        NOT NULL DEFAULT 'open',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One recording per conversation at a time. This is what makes "ai start"
-- while already recording, and two people sending "ai end" at once, resolve
-- in the database instead of in a race between webhook handlers.
CREATE UNIQUE INDEX IF NOT EXISTS ux_capture_open_per_conv
    ON capture_sessions (conversation_id) WHERE status = 'open';

-- v1 -> v2: the trigger used to be "reply to a message and tag the bot", so a
-- request was keyed on that anchor message plus a ±N context window. It is now
-- keyed on a capture session. Old rows are set aside rather than dropped —
-- they hold the URLs of issues that really were published.
DO $$
BEGIN
    IF to_regclass('public.issue_requests') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE  table_schema = current_schema()
             AND  table_name   = 'issue_requests'
             AND  column_name  = 'capture_session_id'
       )
    THEN
        ALTER TABLE issue_requests RENAME TO issue_requests_anchor_v1;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS issue_requests (
    id                  BIGSERIAL PRIMARY KEY,
    capture_session_id  BIGINT      NOT NULL UNIQUE REFERENCES capture_sessions (id),
    conversation_id     TEXT        NOT NULL,
    requested_by        TEXT,
    status              TEXT        NOT NULL DEFAULT 'pending', -- pending|summarized|done|failed
    message_count       INT,
    summary_json        JSONB,
    github_issue_number INT,
    issue_url           TEXT,
    error               TEXT,
    claimed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS line_profiles (
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    display_name    TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, user_id)
);
