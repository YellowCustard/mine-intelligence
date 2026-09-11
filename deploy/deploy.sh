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
  echo "    !!! Edit .env NOW and change POSTGRES_PASSWORD and MINIO_ROOT_PASSWORD before going further."
  echo "    Re-run this script once .env is set."
  exit 1
fi

# Broker auth (brief §10/§11): when a service account is configured, generate the
# Mosquitto password + ACL files from the devices table and run the broker with
# allow_anonymous=false. The API migrates the DB on start, so bring it up first,
# generate the files into the dir it shares with the broker, then select the
# hardened broker config for the full bring-up.
if [ -n "${MM_MQTT_USERNAME:-}" ]; then
  echo "==> Broker auth enabled (MM_MQTT_USERNAME set) — generating broker credentials"
  docker compose up -d --build db api
  echo "    Waiting for the API (runs migrations on start) to be healthy"
  for i in $(seq 1 30); do
    if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)" >/dev/null 2>&1; then
      break
    fi
    sleep 2
    [ "$i" = "30" ] && { echo "    API did not become healthy — check: docker compose logs api"; exit 1; }
  done
  docker compose exec -T api uv run python -m minemonitor.devices.broker_config
  export MM_MOSQUITTO_CONF=./docker/mosquitto.auth.conf
  echo "    Broker will use the hardened config ($MM_MOSQUITTO_CONF)."
fi

echo "==> Building and starting the core stack (db, mqtt, minio, api, ingestor)"
docker compose up -d --build

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
if [ -n "${MM_MQTT_USERNAME:-}" ]; then
  echo
  echo "    Broker auth is on. After provisioning or disabling devices"
  echo "    (POST /sites/{id}/devices), regenerate the broker files and reload it:"
  echo "        docker compose exec api uv run python -m minemonitor.devices.broker_config"
  echo "        docker compose exec mqtt kill -HUP 1"
fi
