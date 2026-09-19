"""Dahua NVR AI-event replay — the simulator-first driver for NVR smart events.

Mirrors the "build against a simulator, not hardware" rule (brief §10) that governs
:mod:`simulator`: the whole NVR ingest path — normalise → dedup → land in the alarm queue —
is developed and tested here against **recorded** NVR events, so it is ready the moment the
Dahua backdoor-API credentials arrive. The live poller/subscriber is a thin driver added
later; it will call the same :func:`ingest_nvr_events`, and the normaliser and its tests do
not change.

A raw NVR event references a camera by ``channel`` (and, in these fixtures, a ``camera``
name). We resolve that name against the **camera registry** (FP-01) to pick up the camera's
operational ``zone_id`` — so an NVR intrusion lands on the same zone a GNSS breach would. An
unregistered camera still ingests (``zone_id`` unresolved), it is just less contextual.

Ingest is **idempotent**: the ``event_id`` is deterministic (see :func:`normalise_nvr_event`),
so replaying a batch — or backfilling after a link outage — never double-alarms.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.contracts.vision import VisionVendorEventV1
from minemonitor.events.repository import persist_event
from minemonitor.ingest.adapters.nvr import build_vendor_event, normalise_nvr_event
from minemonitor.storage.models import Camera, Event, VisionVendorEvent

log = logging.getLogger("minemonitor.ingest.nvr")


def _find_camera_by_name(session: Session, site_id: str, name: str) -> Camera | None:
    stmt = select(Camera).where(Camera.site_id == site_id, Camera.name == name).limit(1)
    return session.execute(stmt).scalars().first()


def _persist_vendor_event(session: Session, vev: VisionVendorEventV1, now: datetime) -> None:
    """Store the normalised vendor record write-once (idempotent on its deterministic id)."""
    if session.get(VisionVendorEvent, vev.vendor_event_id) is not None:
        return
    session.add(
        VisionVendorEvent(
            id=vev.vendor_event_id,
            site_id=vev.site_id,
            source_system=vev.source_system,
            source_event_id=vev.source_event_id,
            camera_id=vev.camera_id,
            channel=vev.channel,
            vendor_type=vev.vendor_type,
            normalized_type=vev.normalized_type,
            vendor_confidence=vev.vendor_confidence,
            vendor_rule_name=vev.vendor_rule_name,
            ts=vev.ts,
            clip_ref=vev.clip_ref,
            created_at=now,
        )
    )


def ingest_nvr_events(
    session: Session,
    site_id: str,
    raw_events: list[dict[str, Any]],
    *,
    commit: bool = True,
) -> list[EventV1]:
    """Normalise and persist a batch of raw NVR AI events. Returns the newly-added events.

    Site-scoped; resolves each event's camera (by name) to its registered ``zone_id`` where
    the camera is in the registry. Deduplicates on the deterministic ``event_id`` both against
    already-stored events and within this batch, so replays and duplicates are no-ops. A
    malformed event raises ``ValueError`` from the normaliser and aborts the batch before any
    commit — bad device data never lands half-ingested.
    """
    now = datetime.now(UTC)
    new_events: list[EventV1] = []
    seen: set[str] = set()
    for raw in raw_events:
        camera_name = raw.get("camera")
        zone_id: str | None = None
        camera_id: str | None = None
        if camera_name:
            cam = _find_camera_by_name(session, site_id, str(camera_name))
            if cam is not None:
                zone_id = cam.zone_id
                camera_id = cam.id
        ev = normalise_nvr_event(raw, site_id=site_id, zone_id=zone_id, camera_name=camera_name)
        if ev.event_id in seen:
            continue  # duplicate within this batch
        if session.get(Event, ev.event_id) is not None:
            continue  # already ingested (idempotent replay/backfill)
        # Store the vendor-inferred record (VISION_BUILD_GATE §4.5), then promote to the
        # unified alarm queue. Both share the deterministic id, so replay/backfill is a no-op.
        _persist_vendor_event(
            session, build_vendor_event(raw, site_id=site_id, camera_id=camera_id), now
        )
        persist_event(session, ev)
        seen.add(ev.event_id)
        new_events.append(ev)
    if commit:
        session.commit()
    log.info(
        "nvr events ingested",
        extra={"site_id": site_id, "received": len(raw_events), "new": len(new_events)},
    )
    return new_events


# A small recorded set — the fixture role the simulator plays for GNSS. Times are explicit
# Africa/Harare offsets (+02:00); ``camera`` names match the registry a site would create.
# Note there is deliberately no identity field anywhere: NVR AI here is count/scene-level.
SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "id": "evt-1001",
        "type": "CrossRegionDetection",
        "channel": 3,
        "camera": "CAM-03 Gold Room",
        "time": "2026-09-12T22:14:07+02:00",
        "object": "person",
        "confidence": 0.91,
        "rule": "GoldRoom-Intrusion",
        "clip": "nvr://ch3/2026-09-12/221407.mp4",
    },
    {
        "id": "evt-1002",
        "type": "CrossLineDetection",
        "channel": 7,
        "camera": "CAM-07 Crusher Walkway",
        "time": "2026-09-12T22:16:41+02:00",
        "object": "person",
        "confidence": 0.78,
        "rule": "Walkway-Tripwire",
    },
    {
        "id": "evt-1003",
        "type": "LoiteringDetection",
        "channel": 3,
        "camera": "CAM-03 Gold Room",
        "time": "2026-09-12T22:20:02+02:00",
        "object": "person",
        "confidence": 0.83,
        "rule": "GoldRoom-Loiter",
        "count": 2,
    },
]


def main() -> None:
    """Replay the sample NVR events into the configured database (a local demo)."""
    from minemonitor.config import get_settings
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory

    settings = get_settings()
    configure_logging(settings.log_level)
    factory = get_session_factory()
    with factory() as session:
        added = ingest_nvr_events(session, settings.default_site_id, SAMPLE_EVENTS)
    print(f"ingested {len(added)} NVR events (replay is idempotent — re-running adds 0)")


if __name__ == "__main__":
    main()
