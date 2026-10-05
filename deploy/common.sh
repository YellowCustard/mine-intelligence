# Steps shared by deploy.sh and restore.sh. Sourced from the repo root; not run on its own.

# Wait for the API, which runs the migrations on start, to answer inside its container.
mm_wait_for_api() {
  local i
  echo "    Waiting for the API (runs migrations on start) to be healthy"
  for i in $(seq 1 30); do
    if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "    API did not become healthy — check: docker compose logs api"
  return 1
}

# The broker login as the containers see it. Compose takes MM_MQTT_USERNAME from the shell first,
# then .env, and the same value picks the broker config in docker-compose.yml. This script's own
# environment usually has neither, so ask the running API. Prints nothing when the login is unset;
# fails when the API cannot be asked, so a failed lookup never passes for "no broker login" and
# skips the broker files and the anonymous-client check.
mm_mqtt_username() {
  if ! docker compose exec -T api python -c 'import os; print(os.environ.get("MM_MQTT_USERNAME", ""))'; then
    echo "ERROR: could not read MM_MQTT_USERNAME from the API container — check: docker compose logs api" >&2
    return 1
  fi
}

# Write the broker password and ACL files from the devices table, into ./docker/mosquitto, which
# the API shares with the broker. The authenticated broker will not start without them.
mm_generate_broker_files() {
  echo "==> Broker login set (MM_MQTT_USERNAME): writing the broker password and ACL files"
  docker compose exec -T api uv run python -m minemonitor.devices.broker_config
}

# Prove the running broker turns away a client with no login: an anonymous publish must come back
# "not authorised". Retries while the broker starts.
mm_check_broker_refuses_anonymous() {
  local i out=""
  echo "==> Checking the broker refuses clients with no login"
  for i in $(seq 1 15); do
    if out=$(docker compose exec -T mqtt mosquitto_pub -h 127.0.0.1 -t healthcheck -m ok 2>&1); then
      echo "ERROR: MM_MQTT_USERNAME is set but the broker accepted a client with no login."
      echo "       MM_MOSQUITTO_CONF (in .env or the shell) points the broker at a config that"
      echo "       allows anonymous clients. Remove it, and compose runs docker/mosquitto.auth.conf;"
      echo "       then re-run this script."
      return 1
    fi
    case "$out" in
      *[Nn]"ot authori"*)
        echo "    Broker refuses anonymous clients."
        return 0
        ;;
    esac
    sleep 2
  done
  echo "ERROR: could not reach the broker to confirm it refuses clients with no login:"
  echo "       $out"
  echo "       Check: docker compose logs mqtt"
  return 1
}
