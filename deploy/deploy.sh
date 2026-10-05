#!/usr/bin/env bash
# One-command deploy for Mine Monitor on a Docker-capable VPS (e.g. xneelo Cloud).
# Run this ON THE SERVER, from the repo root, after: git clone / git pull.
#
#   bash deploy/deploy.sh            # bring the stack up (no demo fleet)
#   bash deploy/deploy.sh --demo     # also start the simulated fleet
#
# It is idempotent — safe to re-run to apply updates.
set -euo pipefail
cd "$(dirname "$0")/.."

DEMO=0
[ "${1:-}" = "--demo" ] && DEMO=1

echo "==> Checking prerequisites"
command -v docker >/dev/null || { echo "ERROR: Docker Engine is not installed. See deploy/DEPLOY.md"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: the Docker Compose plugin is not installed."; exit 1; }

if [ ! -f .env ]; then
  echo "==> No .env found — creating from .env.example"
  cp .env.example .env
  echo "    !!! Edit .env NOW and change POSTGRES_PASSWORD before going further."
  echo "    Re-run this script once .env is set."
  exit 1
fi

. deploy/common.sh

# The database first: a new db image needs its extension updated before the API connects. The
# API migrates the database on start, and the broker files are written from the database, so the
# API comes up next.
echo "==> Starting the database"
docker compose up -d db
mm_wait_for_db || exit 1
mm_update_timescaledb || exit 1

echo "==> Starting the API"
docker compose up -d --build db api
mm_wait_for_api || exit 1

# Broker auth (brief §10/§11). docker-compose.yml runs the authenticated broker
# (docker/mosquitto.auth.conf, allow_anonymous false) whenever MM_MQTT_USERNAME is set, in .env or
# the shell. It needs the password and ACL files, so write them before the broker starts.
MQTT_USER=$(mm_mqtt_username)
if [ -n "$MQTT_USER" ]; then
  mm_generate_broker_files
fi

echo "==> Building and starting the core stack (db, mqtt, api, ingestor)"
docker compose up -d --build

if [ -n "$MQTT_USER" ]; then
  mm_check_broker_refuses_anonymous || exit 1
fi

if [ "$DEMO" = "1" ]; then
  echo "==> Starting the simulated fleet (demo data)"
  docker compose --profile sim up -d --build simulator
fi

echo "==> Waiting for the API to become healthy"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "    API healthy."
    break
  fi
  sleep 2
  [ "$i" = "30" ] && { echo "    API did not become healthy in time — check: docker compose logs api"; exit 1; }
done

echo
echo "==> Up. The dashboard is served by the API on port 8000 (localhost on the VPS)."
echo "    The API has HTTP Basic auth (M6). If you did not set MM_BOOTSTRAP_ADMIN_* in"
echo "    .env, create the first admin now:"
echo "        docker compose exec api uv run python -m minemonitor.auth.cli admin admin"
echo "    Basic credentials must ride TLS — do NOT expose plain port 8000 publicly."
echo "    View it safely with an SSH tunnel from your laptop:"
echo "        ssh -L 8000:127.0.0.1:8000 <user>@<vps-host>"
echo "    then open http://localhost:8000/"
echo
echo "    For a public URL, put it behind nginx + TLS (see deploy/DEPLOY.md)."
if [ -n "$MQTT_USER" ]; then
  echo
  echo "    Broker auth is on. After provisioning or disabling devices"
  echo "    (POST /sites/{id}/devices), regenerate the broker files and reload it:"
  echo "        docker compose exec api uv run python -m minemonitor.devices.broker_config"
  echo "        docker compose exec mqtt kill -HUP 1"
fi
