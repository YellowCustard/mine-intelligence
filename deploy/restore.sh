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
# It brings up ONLY the database, waits for it to be healthy, loads the dump, then starts the API.
# With broker auth on (MM_MQTT_USERNAME set), it writes the broker password and ACL files from the
# restored devices table, which a clean box does not have yet. Then it starts the rest of the
# stack. Accepts a plain .sql or a gzipped .sql.gz dump.
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
. deploy/common.sh

echo "==> Starting the database only"
docker compose up -d db

# Over TCP (mm_wait_for_db): on a clean box the image first initialises the database with a
# socket-only server and then restarts it, and a dump loaded into that first server is cut off.
mm_wait_for_db || exit 1

echo "==> Loading $DUMP into '$PGDB'"
case "$DUMP" in
  *.gz) gunzip -c "$DUMP" | docker compose exec -T db psql -U "$PGUSER" "$PGDB" ;;
  *)    docker compose exec -T db psql -U "$PGUSER" "$PGDB" < "$DUMP" ;;
esac

echo "==> Starting the API"
docker compose up -d --build db api
mm_wait_for_api || exit 1
MQTT_USER=$(mm_mqtt_username) || exit 1
if [ -n "$MQTT_USER" ]; then
  mm_generate_broker_files
fi

echo "==> Starting the rest of the stack"
docker compose up -d --build
if [ -n "$MQTT_USER" ]; then
  mm_check_broker_refuses_anonymous || exit 1
fi

echo "==> Restore complete. Check: docker compose ps && curl -fsS http://127.0.0.1:8000/health"
