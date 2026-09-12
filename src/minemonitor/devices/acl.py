"""Render a Mosquitto ACL from the devices table (brief §10).

The broker is the authority that stops a device publishing another asset's telemetry:
each device authenticates with its ``device_id`` as the MQTT username, and this ACL
restricts that username to writing only its own ``mm/<site>/<asset>/position`` topic.
Regenerate and reload the broker whenever devices change:

    docker compose exec api uv run python -m minemonitor.devices.acl > mosquitto/acl
    docker compose exec mqtt kill -HUP 1
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.storage.db import get_session_factory
from minemonitor.storage.models import Device


def render_mosquitto_acl(
    session: Session, *, prefix: str = "mm", ingest_user: str = "mm-ingestor"
) -> str:
    """Return the ACL text: the ingestor may read everything; each device writes one topic."""
    lines = [
        "# Generated from the devices table — do not edit by hand.",
        "# The ingestor subscribes to all telemetry; each device writes only its own topic.",
        "",
        f"user {ingest_user}",
        f"topic read {prefix}/#",
        "",
    ]
    devices = session.execute(
        select(Device).where(Device.enabled.is_(True)).order_by(Device.device_id)
    ).scalars()
    for d in devices:
        lines += [
            f"user {d.device_id}",
            f"topic write {prefix}/{d.site_id}/{d.asset_id}/position",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    settings = get_settings()
    session = get_session_factory()()
    try:
        sys.stdout.write(
            render_mosquitto_acl(
                session,
                prefix=settings.mqtt_topic_prefix,
                ingest_user=settings.mqtt_ingest_client_id,
            )
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
