"""Render a Mosquitto ACL from the devices table (brief §10).

The broker is the authority that stops a device publishing another asset's telemetry:
each device authenticates with its ``device_id`` as the MQTT username, and this ACL
restricts that username to writing only its own ``mm/<site>/<asset>/position`` topic.

A vision edge node is a device too (``source = "vision-edge"``, its ``asset_id`` is the node id).
It writes ``mm/<site>/vision/#`` for its own site only, and never a position topic; the ingestor
additionally rejects any record whose topic and payload disagree.

Regenerate and reload the broker whenever devices change (this writes the ACL and the password
file together):

    docker compose exec api uv run python -m minemonitor.devices.broker_config
    docker compose exec mqtt kill -HUP 1
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.storage.db import get_session_factory
from minemonitor.storage.models import Device

VISION_EDGE_SOURCE = "vision-edge"


def render_mosquitto_acl(
    session: Session, *, prefix: str = "mm", ingest_user: str = "mm-ingestor"
) -> str:
    """Return the ACL text: the service account reads and writes everything; each device writes
    one topic.

    ``ingest_user`` is the service account's login (MM_MQTT_USERNAME). The ingestor subscribes
    with it, and the simulator and the Teltonika adapter publish every asset's positions with it,
    so it needs both. The broker matches ``user`` lines against the login, never the client id.
    """
    lines = [
        "# Generated from the devices table — do not edit by hand.",
        "# The service account (ingestor, simulator, Teltonika adapter) reads and writes all",
        "# telemetry; each device writes only its own topic.",
        "",
        f"user {ingest_user}",
        f"topic readwrite {prefix}/#",
        "",
    ]
    devices = session.execute(
        select(Device).where(Device.enabled.is_(True)).order_by(Device.device_id)
    ).scalars()
    for d in devices:
        topic = (
            f"{prefix}/{d.site_id}/vision/#"
            if d.source == VISION_EDGE_SOURCE
            else f"{prefix}/{d.site_id}/{d.asset_id}/position"
        )
        lines += [f"user {d.device_id}", f"topic write {topic}", ""]
    return "\n".join(lines)


def main() -> int:
    settings = get_settings()
    if not settings.mqtt_username:
        sys.stderr.write("MM_MQTT_USERNAME is not set — the broker service account is undefined.\n")
        return 2
    session = get_session_factory()()
    try:
        sys.stdout.write(
            render_mosquitto_acl(
                session,
                prefix=settings.mqtt_topic_prefix,
                ingest_user=settings.mqtt_username,
            )
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
