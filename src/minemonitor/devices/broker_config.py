"""Render the Mosquitto password and ACL files from the devices table (brief §10/§11).

The broker is the transport-layer authority that stops a device publishing another
asset's telemetry. Enforcing that requires three things the app now owns:

1. a **password file** — the ingest service account plus one line per enabled
   device, each an ``mosquitto_passwd``-compatible ``$7$`` PBKDF2-SHA512 hash;
2. an **ACL file** — the service account may read/write everything; each device may
   write only its own ``mm/<site>/<asset>/position`` topic (see :mod:`.acl`);
3. a broker configured with ``allow_anonymous false`` and those two files
   (``docker/mosquitto.auth.conf``).

The ``$7$`` format is self-describing — ``$7$<iterations>$<b64 salt>$<b64 hash>`` —
so the broker recomputes PBKDF2-SHA512 with the embedded salt and iteration count and
compares. Generating it here (pure Python, no ``mosquitto_passwd`` binary in the API
image) keeps provisioning a single API call; the authenticated integration test
proves a real broker accepts what we emit.

Regenerate and reload the broker whenever devices change:

    docker compose exec api uv run python -m minemonitor.devices.broker_config
    docker compose exec mqtt kill -HUP 1
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.devices.acl import render_mosquitto_acl
from minemonitor.storage.db import get_session_factory
from minemonitor.storage.models import Device

# PBKDF2-SHA512 work factor. The broker reads the count from each line, so this may
# be raised over time without invalidating existing hashes.
_ITERATIONS = 100_000
_SALT_BYTES = 12
_DKLEN = 64  # SHA-512 output width, as mosquitto_passwd uses


def mosquitto_password_hash(
    password: str, *, iterations: int = _ITERATIONS, salt: bytes | None = None
) -> str:
    """Return a Mosquitto ``$7$`` PBKDF2-SHA512 hash line body for ``password``.

    ``salt`` is only a parameter so tests can pin it; production always uses a fresh
    random salt.
    """
    salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, dklen=_DKLEN)
    b64_salt = base64.b64encode(salt).decode()
    b64_hash = base64.b64encode(dk).decode()
    return f"$7${iterations}${b64_salt}${b64_hash}"


def render_password_file(session: Session, *, service_user: str, service_hash: str) -> str:
    """Return the broker password file: the ingest service account + enabled devices.

    Each device contributes its stored ``broker_pw_hash``; devices without an issued
    secret (or disabled) are omitted, so they cannot authenticate.
    """
    lines = [
        "# Generated from the devices table — do not edit by hand.",
        f"{service_user}:{service_hash}",
    ]
    devices = session.execute(
        select(Device)
        .where(Device.enabled.is_(True), Device.broker_pw_hash.is_not(None))
        .order_by(Device.device_id)
    ).scalars()
    for d in devices:
        lines.append(f"{d.device_id}:{d.broker_pw_hash}")
    return "\n".join(lines) + "\n"


def write_broker_files(
    session: Session,
    *,
    out_dir: str,
    prefix: str,
    service_user: str,
    service_password: str,
) -> tuple[str, str]:
    """Write ``passwd`` and ``acl`` into ``out_dir``; return their paths.

    The service account (ingestor consumer + internal publishers: the simulator and
    the Teltonika adapter) may read and write everything; each device is confined by
    the ACL to its own topic.
    """
    os.makedirs(out_dir, exist_ok=True)
    passwd_path = os.path.join(out_dir, "passwd")
    acl_path = os.path.join(out_dir, "acl")
    passwd = render_password_file(
        session,
        service_user=service_user,
        service_hash=mosquitto_password_hash(service_password),
    )
    acl = render_mosquitto_acl(session, prefix=prefix, ingest_user=service_user)
    with open(passwd_path, "w") as fh:
        fh.write(passwd)
    with open(acl_path, "w") as fh:
        fh.write(acl)
    # The password file holds credential hashes — keep it owner-readable only.
    os.chmod(passwd_path, 0o600)
    return passwd_path, acl_path


def main() -> int:
    settings = get_settings()
    if not settings.mqtt_username:
        sys.stderr.write(
            "MM_MQTT_USERNAME is not set — the broker service account is undefined; "
            "set MM_MQTT_USERNAME/MM_MQTT_PASSWORD before generating broker files.\n"
        )
        return 2
    out_dir = os.environ.get("MM_MOSQUITTO_DIR", "docker/mosquitto")
    session = get_session_factory()()
    try:
        passwd_path, acl_path = write_broker_files(
            session,
            out_dir=out_dir,
            prefix=settings.mqtt_topic_prefix,
            service_user=settings.mqtt_username,
            service_password=settings.mqtt_password,
        )
    finally:
        session.close()
    sys.stderr.write(f"wrote {passwd_path} and {acl_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
