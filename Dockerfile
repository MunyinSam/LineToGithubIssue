FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# No CMD: docker-compose.yml sets `command:` per service. The API and the
# issue-worker are the same image, just different entrypoints.
CMD ["python", "-m", "app.workers.issue_worker"]
