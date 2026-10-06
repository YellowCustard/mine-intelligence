#!/usr/bin/env bash
# Back up the Mine Monitor database to a gzipped SQL dump.
# Run ON THE SERVER, from the repo root. Reads POSTGRES_* from .env.
#
#   bash deploy/backup.sh                 # -> backups/minemonitor_<timestamp>.sql.gz
#   bash deploy/backup.sh /path/to/out.sql.gz
#
# deploy.sh runs it after every deploy. When the dump does not complete it exits non-zero and
# leaves no file behind, so a file under a dump's name is always a whole dump.
#
# Restore a dump with: bash deploy/restore.sh <file.sql.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
PGUSER="${POSTGRES_USER:-minemonitor}"
PGDB="${POSTGRES_DB:-minemonitor}"

OUT="${1:-backups/minemonitor_$(date +%Y%m%d_%H%M%S).sql.gz}"
DIR=$(dirname "$OUT")
mkdir -p "$DIR"

# The dumps usually share a disk with the database, which must not run out of room: refuse
# unless the largest dump already there fits again, a fifth larger, with 1 GB still free after.
largest_kb=$(find "$DIR" -maxdepth 1 -type f -name 'minemonitor_*.sql.gz' -exec du -k {} + | sort -n | tail -n 1 | cut -f1)
need_kb=$((${largest_kb:-0} * 6 / 5 + 1024 * 1024))
avail_kb=$(df -Pk "$DIR" | awk 'NR == 2 { print $4 }')
if [ "$avail_kb" -lt "$need_kb" ]; then
  echo "ERROR: $((avail_kb / 1024)) MB free in $DIR, and a dump needs $((need_kb / 1024)) MB"
  echo "       (the largest dump there is $((${largest_kb:-0} / 1024)) MB; 1 GB stays free for the database)."
  echo "       Copy older dumps off the box and delete them, then run this again."
  exit 1
fi

echo "==> Dumping database '$PGDB' (user '$PGUSER') -> $OUT"
tmp="$OUT.partial"
trap 'rm -f "$tmp"' EXIT
if ! docker compose exec -T db pg_dump -U "$PGUSER" "$PGDB" | gzip > "$tmp"; then
  echo "ERROR: the dump did not complete, so nothing was written to $OUT."
  echo "       Check: docker compose ps db; docker compose logs db"
  exit 1
fi
mv -f "$tmp" "$OUT"
trap - EXIT
echo "==> Wrote $(du -h "$OUT" | cut -f1) to $OUT"
