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


def parse_vision_topic(prefix: str, topic: str) -> tuple[str, str, str] | None:
    """Return ``(site_id, source_id, kind)`` from ``<prefix>/<site>/vision/<source>/<kind>``.

    ``source`` is the camera id (camera-scoped records) or the node id (node health).
    """
    parts = topic.split("/")
    if len(parts) == 5 and parts[0] == prefix and parts[2] == "vision" and all(parts):
        return parts[1], parts[3], parts[4]
    return None


def vision_topic_matches_payload(prefix: str, topic: str, payload: object) -> bool:
    """Anti-spoof for vision: the topic's site and source must match the payload's.

    Camera-scoped records must name the topic's camera; node health must name the topic's node.
    """
    parsed = parse_vision_topic(prefix, topic)
    if parsed is None:
        return False
    site_id, source_id, _kind = parsed
    if getattr(payload, "site_id", None) != site_id:
        return False
    camera_id = getattr(payload, "camera_id", None)
    if camera_id is not None:
        return bool(camera_id == source_id)
    return bool(getattr(payload, "node_id", None) == source_id)
