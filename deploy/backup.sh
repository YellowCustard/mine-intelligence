#!/usr/bin/env bash
# Back up the Mine Monitor database to a gzipped SQL dump.
# Run ON THE SERVER, from the repo root. Reads POSTGRES_* from .env.
#
#   bash deploy/backup.sh                 # -> backups/minemonitor_<timestamp>.sql.gz
#   bash deploy/backup.sh /path/to/out.sql.gz
#
# Restore a dump with: bash deploy/restore.sh <file.sql.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
PGUSER="${POSTGRES_USER:-minemonitor}"
PGDB="${POSTGRES_DB:-minemonitor}"

OUT="${1:-backups/minemonitor_$(date +%Y%m%d_%H%M%S).sql.gz}"
mkdir -p "$(dirname "$OUT")"

echo "==> Dumping database '$PGDB' (user '$PGUSER') -> $OUT"
docker compose exec -T db pg_dump -U "$PGUSER" "$PGDB" | gzip > "$OUT"
echo "==> Wrote $(du -h "$OUT" | cut -f1) to $OUT"
