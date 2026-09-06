# Mine Monitor API image. uv-based, slim, self-hostable (brief §3).
FROM python:3.11-slim

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra dev

# Now the source.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY contracts ./contracts
COPY web ./web
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN uv sync --frozen --extra dev && chmod +x ./docker/entrypoint.sh

EXPOSE 8000

# Liveness: the API is up and its database is reachable. No curl in slim; use
# Python. /healthz returns 503 (raising HTTPError) when the DB is unreachable.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)"]

ENTRYPOINT ["./docker/entrypoint.sh"]
