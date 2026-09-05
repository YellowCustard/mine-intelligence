#!/usr/bin/env bash
# Restore Mine Monitor onto a clean box from the compose file + a database dump.
# This is the M6 acceptance path: "restore onto a clean box from the compose file
# and a database dump, and it runs."
#
# Run ON THE SERVER, from the repo root, with .env already configured to match the
# source box's POSTGRES_* values:
#
#   bash deploy/restore.sh backups/minemonitor_20260905_120000.sql.gz
#
# It brings up ONLY the database, waits for it to be healthy, loads the dump, then
# starts the rest of the stack. Accepts a plain .sql or a gzipped .sql.gz dump.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:-}"
[ -n "$DUMP" ] || { echo "usage: bash deploy/restore.sh <dump.sql|dump.sql.gz>"; exit 1; }
[ -f "$DUMP" ] || { echo "ERROR: dump not found: $DUMP"; exit 1; }
[ -f .env ] || { echo "ERROR: no .env — copy .env.example and set the same passwords as the source box."; exit 1; }

set -a && . ./.env && set +a
PGUSER="${POSTGRES_USER:-minemonitor}"
PGDB="${POSTGRES_DB:-minemonitor}"

command -v docker >/dev/null || { echo "ERROR: Docker Engine is not installed. See deploy/DEPLOY.md"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: the Docker Compose plugin is not installed."; exit 1; }

echo "==> Starting the database only"
docker compose up -d db

echo "==> Waiting for the database to become healthy"
for i in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U "$PGUSER" >/dev/null 2>&1; then
    echo "    Database ready."
    break
  fi
  sleep 2
  [ "$i" = "30" ] && { echo "    Database did not become ready — check: docker compose logs db"; exit 1; }
done

echo "==> Loading $DUMP into '$PGDB'"
case "$DUMP" in
  *.gz) gunzip -c "$DUMP" | docker compose exec -T db psql -U "$PGUSER" "$PGDB" ;;
  *)    docker compose exec -T db psql -U "$PGUSER" "$PGDB" < "$DUMP" ;;
esac

echo "==> Starting the rest of the stack"
docker compose up -d --build

echo "==> Restore complete. Check: docker compose ps && curl -fsS http://127.0.0.1:8000/health"
