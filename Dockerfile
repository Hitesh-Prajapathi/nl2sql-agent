# syntax=docker/dockerfile:1

# ── Builder: resolve and install runtime dependencies into /app/.venv ────────
FROM python:3.15-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# uv.lock is optional in the glob so the build still works if it is missing.
COPY pyproject.toml uv.lock* ./
COPY src/ src/

# Use the lock when present. If it is stale or missing, warn and resolve without it,
# so the image is not reproducible from uv.lock until the lock is refreshed and committed.
RUN uv sync --frozen --no-dev || (echo "WARNING: uv.lock missing or stale; resolving without lock" >&2 && uv sync --no-dev)

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.15-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "nl2sql.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
