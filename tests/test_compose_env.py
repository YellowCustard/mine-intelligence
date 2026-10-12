"""Every setting ``.env.example`` documents reaches the containers that read it.

A container gets only the variables its compose service lists under ``environment``. The
containers never read ``.env`` themselves: it is not in the image (``.dockerignore``) and no
service has an ``env_file``. So a setting an operator puts in ``.env``, as ``.env.example`` and
the release's own compose header say to, is silently ignored unless the service lists it. Most
were: ``MM_ENV=prod`` reached no container, so the production guard never ran on any deploy;
the gate feed (``MM_ACCESS_GATE_URL``) could not be switched on; strict device provisioning,
the breach notice deadlines and the offline threshold kept their defaults whatever ``.env``
said; and the Teltonika listener had no database URL, so every tracker's handshake failed on
the IMEI lookup.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from minemonitor.config import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"

# Documented variables no container reads: compose itself uses them on the host (published
# addresses, the broker config file), or a one-off command takes them (the user CLI's password,
# passed with `docker compose exec -e`), or the vision image fixes them.
_HOST_SIDE = {
    "MM_API_BIND",
    "MM_DB_BIND",
    "MM_MQTT_BIND",
    "MM_OBJECTSTORE_BIND",
    "MM_TELTONIKA_BIND",
    "MM_MOSQUITTO_CONF",
    "MM_NEW_USER_PASSWORD",
    "MM_EDGE_MODELS_DIR",
    "MM_EDGE_STATE_DIR",
}
# Compose sets these for the container; a value in .env is not meant to reach it. The database
# URL is built from POSTGRES_*, and the broker is the compose service.
_FIXED = {"MM_DATABASE_URL", "MM_MQTT_HOST"}
# The tracker listener's own settings go to it alone, and the publisher spool to the services
# that publish (the listener and the simulator).
_LISTENER = {"MM_TELTONIKA_PORT", "MM_TELTONIKA_IDLE_TIMEOUT_S"}
_PUBLISHER = {"MM_SPOOL_PATH"}
# A secret goes only where it is used.
_WITHHELD = {
    ("api", "MM_ACCESS_GATE_TOKEN"),  # only the ingestor polls the gate
    ("ingestor", "MM_BOOTSTRAP_ADMIN_USER"),  # only the API creates the first admin
    ("ingestor", "MM_BOOTSTRAP_ADMIN_PASSWORD"),
}
_MQTT = {
    "MM_MQTT_HOST",
    "MM_MQTT_PORT",
    "MM_MQTT_TOPIC_PREFIX",
    "MM_MQTT_USERNAME",
    "MM_MQTT_PASSWORD",
}


def _documented() -> set[str]:
    """Every MM_ variable .env.example sets, including the ones it shows commented out."""
    return set(re.findall(r"^#? ?(MM_[A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.M))


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())["services"]
    return services


def _environment(service: str) -> dict[str, str]:
    env = _services()[service].get("environment", {})
    assert isinstance(env, dict), f"{service}: environment must be a mapping"
    return {k: str(v) for k, v in env.items()}


def _platform() -> set[str]:
    """Documented settings of the platform itself, which the API and the ingestor both run."""
    fields = {f"MM_{name.upper()}" for name in Settings.model_fields}
    return (_documented() & fields) - _LISTENER - _PUBLISHER


def test_the_api_and_the_ingestor_get_every_platform_setting() -> None:
    missing = {
        service: sorted(
            v for v in _platform() - set(_environment(service)) if (service, v) not in _WITHHELD
        )
        for service in ("api", "ingestor")
    }
    assert missing == {"api": [], "ingestor": []}


def test_a_secret_goes_only_where_it_is_used() -> None:
    leaked = [(s, v) for s, v in sorted(_WITHHELD) if v in _environment(s)]
    assert leaked == []


def test_each_setting_is_taken_from_env_not_written_into_compose() -> None:
    # `MM_ENV: prod` in compose would pass the check above but ignore the operator's .env.
    hardcoded = [
        (service, key, value)
        for service in _services()
        for key, value in _environment(service).items()
        if key in _documented() - _FIXED and f"${{{key}" not in value
    ]
    assert hardcoded == []


def test_the_tracker_listener_reaches_the_device_table() -> None:
    env = _environment("teltonika")
    wanted = {
        "MM_ENV",
        "MM_LOG_LEVEL",
        "MM_DATABASE_URL",
        "MM_DEFAULT_SITE_ID",
        "MM_MQTT_REQUIRE_REGISTERED_DEVICE",
        *_MQTT,
        *_LISTENER,
        *_PUBLISHER,
    }
    assert sorted(wanted - set(env)) == []
    # The IMEI lookup reads the same database the platform writes devices to.
    assert env["MM_DATABASE_URL"] == _environment("api")["MM_DATABASE_URL"]
    assert _services()["teltonika"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_every_service_on_the_broker_uses_the_same_topics() -> None:
    # A publisher on the default prefix and an ingestor on the configured one never meet.
    for service in ("api", "ingestor", "teltonika", "simulator"):
        env = _environment(service)
        assert sorted(_MQTT - set(env)) == [], service
        assert "MM_DEFAULT_SITE_ID" in env, service
    for service in ("teltonika", "simulator"):
        assert sorted(_PUBLISHER - set(_environment(service))) == [], service


def test_every_documented_variable_reaches_a_container_or_is_read_on_the_host() -> None:
    # Under its own name, or interpolated into another (Caddy's MM_TLS_ISSUER takes MM_TLS_EMAIL).
    passed = {
        name
        for service in _services()
        for key, value in _environment(service).items()
        for name in (key, *re.findall(r"\$\{(MM_[A-Z0-9_]+)", value))
    }
    assert sorted(_documented() - passed - _HOST_SIDE) == []
    # And the host-side list holds nothing a container does get.
    assert sorted(_HOST_SIDE & passed) == []
