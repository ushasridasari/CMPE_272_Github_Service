# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build wheels for the runtime dependencies.
# Compilers and build headers stay in this stage and never reach the final
# image, which keeps the runtime small and shrinks the attack surface.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Fail fast on unflushed logs, and never write .pyc into the layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    EVENTS_DB_PATH=/data/events.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user. A container that does not need root should not
# have it, and the webhook endpoint is internet-facing by design.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY app/ ./app/
COPY openapi.yaml ./

# The SQLite event log lives on a volume so deliveries survive a restart.
RUN mkdir -p /data && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Not `python -m app.main`: uvicorn is PID 1 here so it receives SIGTERM
# directly and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
