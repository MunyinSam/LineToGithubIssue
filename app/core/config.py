from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LINE
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # PVSD
    line_queue_name: str = "line_notify"

    # RabbitMQ
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    issue_queue_name: str = "line.issue"

    # Postgres
    pgvector_host: str = "127.0.0.1"
    pgvector_port: int = 5433
    pgvector_database: str = "line"
    pgvector_user: str = "postgres"
    pgvector_password: str = ""
    pgvector_pool_min_size: int = 2
    pgvector_pool_max_size: int = 10

    # Ollama
    ai_api: str = "http://localhost:11434"
    chat_model: str = "qwen3.5:9b"
    embedding_model: str = "qwen3-embedding:4b"
    ollama_keep_alive: str = "30m"
    # Pinned: varying num_ctx between calls forces a full model reload (~60s).
    ollama_num_ctx: int = 32768
    # Thinking off. qwen3.5's reasoning tokens count against num_predict but
    # are NOT part of the JSON we asked for, so a long think starved the body
    # and raised SummarizerTruncated — non-deterministically, which is why it
    # looked flaky rather than broken. Measured on a real 3-message capture:
    # think=on burns ~16k reasoning chars and ~55s per call; think=off answers
    # in ~5s using ~300 of the 6000 tokens. Triage from a transcript is
    # extraction, not reasoning, so nothing of value is lost.
    ollama_think: bool = False
    # With thinking off this is ~20x more headroom than a summary needs; it
    # stays generous so a long capture can't clip the JSON.
    ollama_num_predict: int = 6000
    ollama_timeout_s: int = 180

    # Capture sessions ("ai start" ... "ai end")
    # An open session that nobody ended is expired on the next "ai start" so
    # it can't swallow hours of unrelated chat.
    capture_session_ttl_minutes: int = 120
    # Hard cap on how much one recording can feed the model, in case someone
    # leaves a session open through a busy stretch of the group.
    max_capture_messages: int = 200
    # How long the worker waits for images/files in the capture to finish
    # downloading in the API process before summarizing without them.
    attachment_wait_s: int = 20

    # GitHub
    github_token: str = ""  # default/fallback — used when the reporter has no mapped token
    github_user_tokens: str = ""  # "DisplayName:token,DisplayName:token" — per-reporter override
    github_owner: str = ""
    github_repo: str = ""
    github_default_labels: str = "needs-triage"
    # Applied to every auto-filed issue on top of whatever the model picks, so
    # AI-generated issues stay visually distinct from hand-filed ones.
    github_auto_label: str = "AI"
    github_api_base: str = "https://api.github.com"
    # Captured images/files are uploaded as assets on this one release and
    # linked from the issue body — GitHub has no issue-attachment API. Needs
    # 'Contents: Read and write' on the token, on top of Issues; turn this off
    # to keep filing issues (files are still captured to disk either way).
    attach_files_to_issues: bool = True
    github_assets_tag: str = "line-attachments"
    github_upload_timeout_s: int = 120
    # Each asset is read into memory before upload, so cap it. LINE itself
    # tops out well below this for most files.
    github_asset_max_bytes: int = 25 * 1024 * 1024
    max_attachments_per_issue: int = 20

    # Storage / runtime
    media_root: str = "./data/media"
    stale_claim_minutes: int = 10
    # Publicly reachable origin for this app (nginx/DDNS in front of it), used
    # to build attachment preview URLs that GitHub can fetch and inline-render
    # in an issue body. No trailing slash.
    public_base_url: str = ""


    @property
    def pgvector_conninfo(self) -> str:
        return (
            f"host={self.pgvector_host} "
            f"port={self.pgvector_port} "
            f"dbname={self.pgvector_database} "
            f"user={self.pgvector_user} "
            f"password={self.pgvector_password}"
        )

    @property
    def github_labels(self) -> list[str]:
        return [x.strip() for x in self.github_default_labels.split(",") if x.strip()]

    @property
    def github_token_by_name(self) -> dict[str, str]:
        """Maps a LINE display name to the GitHub token that should file
        issues reported by them, so the issue's creator matches whoever
        actually ran 'ai end'. Anyone not listed here uses github_token."""
        mapping = {}
        for pair in self.github_user_tokens.split(","):
            if ":" not in pair:
                continue
            name, _, token = pair.partition(":")
            name = name.strip()
            token = token.strip()
            if name and token:
                mapping[name] = token
        return mapping


settings = Settings()
