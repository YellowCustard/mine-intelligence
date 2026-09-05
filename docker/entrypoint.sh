#!/usr/bin/env bash
# Run migrations, then serve. Migrations on start keep the DB in step with the
# image; safe because Alembic is idempotent per revision.
set -euo pipefail

echo "Running database migrations..."
uv run alembic upgrade head

echo "Starting API..."
exec uv run uvicorn minemonitor.api.main:app --host 0.0.0.0 --port 8000
