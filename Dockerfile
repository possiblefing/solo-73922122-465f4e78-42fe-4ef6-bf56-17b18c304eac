FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARCHIVE_DB_PATH=/data/archive.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data

# The management entrypoint runs two uvicorn worker processes.
EXPOSE 8000
# The read-only public entrypoint runs two uvicorn worker processes.
EXPOSE 8080

# Compose defines a per-service healthcheck against the correct port.

# docker compose supplies either `management` or `public` (see app/serve.py).
CMD ["python", "-m", "app.serve", "management"]
