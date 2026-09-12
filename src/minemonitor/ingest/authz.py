"""Ingest-boundary authorization for MQTT telemetry (brief §10).

The MQTT topic ``<prefix>/<site>/<asset>/position`` names *which* asset a message is
for; the payload also carries ``site_id``/``asset_id``. A device (confined by the
broker ACL to its own topic) therefore cannot spoof another asset by editing the
body — the ingestor rejects any message whose topic and payload disagree, mirroring
the object-level check the HTTP ingest endpoint already applies.
"""

from __future__ import annotations


def parse_position_topic(prefix: str, topic: str) -> tuple[str, str] | None:
    """Return ``(site_id, asset_id)`` from a position topic, or ``None`` if malformed."""
    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == prefix and parts[3] == "position" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def topic_matches_payload(prefix: str, topic: str, site_id: str, asset_id: str) -> bool:
    """True if the topic's site/asset match the payload's — the anti-spoof check."""
    return parse_position_topic(prefix, topic) == (site_id, asset_id)
