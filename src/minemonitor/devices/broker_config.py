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
import contextlib
import hashlib
import os
import secrets
import sys
import tempfile

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

# The broker image (eclipse-mosquitto) starts as root, drops to its mosquitto user (uid/gid 1883),
# and only then opens the password and ACL files. It cannot chown them itself because they are
# mounted read-only, so files left owned by root make the broker exit at start.
BROKER_UID = 1883
BROKER_GID = 1883


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


def broker_file_owner() -> tuple[int, int] | None:
    """The owner to give the broker files: the broker's user when we can chown (root), else none.

    The API container runs as root. Run by hand as an ordinary user (a dev box), the files stay
    with that user.
    """
    return (BROKER_UID, BROKER_GID) if os.geteuid() == 0 else None


def _replace_private(path: str, content: str, owner: tuple[int, int] | None) -> None:
    """Replace ``path`` with a complete 0600 file in one rename.

    The broker re-reads the file on SIGHUP and the site can lose power mid-write, so it must
    never see a truncated file: write a temp file beside it, fsync, then rename over.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp creates it 0600
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if owner is not None:
            os.chown(tmp, *owner)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_broker_files(
    session: Session,
    *,
    out_dir: str,
    prefix: str,
    service_user: str,
    service_password: str,
    owner: tuple[int, int] | None = None,
) -> tuple[str, str]:
    """Write ``passwd`` and ``acl`` into ``out_dir``; return their paths.

    The service account (ingestor consumer + internal publishers: the simulator and
    the Teltonika adapter) may read and write everything; each device is confined by
    the ACL to its own topic. Both files are 0600 (the password file holds credential
    hashes), owned by ``owner`` (uid, gid) when given.
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
    _replace_private(passwd_path, passwd, owner)
    _replace_private(acl_path, acl, owner)
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
            owner=broker_file_owner(),
        )
    finally:
        session.close()
    sys.stderr.write(f"wrote {passwd_path} and {acl_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
