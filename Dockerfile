FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY app/ app/

# No CMD: docker-compose.yml sets `command:` per service. The API and the
# issue-worker are the same image, just different entrypoints.
CMD ["python", "-m", "app.workers.issue_worker"]
