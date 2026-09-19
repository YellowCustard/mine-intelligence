"""Dahua NVR AI-event ingest: normalisation, the no-identity boundary, and idempotency.

The acceptance bar mirrors M3's for GNSS geofencing: a real detection raises exactly one
alarm, a replay raises none, malformed input is rejected loudly, and no identity ever lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from minemonitor.cameras.service import create_camera
from minemonitor.events.repository import list_events
from minemonitor.ingest.adapters.dahua_nvr_sim import SAMPLE_EVENTS, ingest_nvr_events
from minemonitor.ingest.adapters.nvr import normalise_nvr_event

SITE = "kn-zw-01"
_NOW = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "evt-1",
        "type": "CrossRegionDetection",
        "channel": 3,
        "camera": "CAM-03 Gold Room",
        "time": "2026-09-12T22:14:07+02:00",
        "object": "person",
        "confidence": 0.9,
    }
    base.update(over)
    return base


# -- normalisation ---------------------------------------------------------------------


def test_intrusion_maps_to_critical_zone_breach() -> None:
    ev = normalise_nvr_event(_raw(), site_id=SITE, zone_id="gold-room", camera_name="CAM-03")
    assert ev.type == "zone_breach" and ev.severity == "critical"
    assert ev.zone_id == "gold-room" and ev.asset_id is None
    assert ev.source == "nvr:CAM-03" and ev.advisory is True
    assert ev.detail is not None and ev.detail["nvr_event_type"] == "CrossRegionDetection"
    # Device time preserved as an instant (22:14:07 +02:00 == 20:14:07Z).
    assert ev.ts.astimezone(UTC).hour == 20


def test_tripwire_and_loiter_severities() -> None:
    line = normalise_nvr_event(_raw(type="CrossLineDetection"), site_id=SITE)
    loiter = normalise_nvr_event(_raw(type="LoiteringDetection"), site_id=SITE)
    assert (line.type, line.severity) == ("zone_breach", "warning")
    assert (loiter.type, loiter.severity) == ("zone_dwell", "warning")


def test_event_id_is_deterministic_for_idempotency() -> None:
    a = normalise_nvr_event(_raw(), site_id=SITE)
    b = normalise_nvr_event(_raw(), site_id=SITE)
    assert a.event_id == b.event_id == f"nvr-{SITE}-3-evt-1"


def test_clip_is_a_reference_in_evidence_only() -> None:
    ev = normalise_nvr_event(_raw(clip="nvr://ch3/x.mp4"), site_id=SITE)
    assert ev.evidence is not None and ev.evidence["clip_uri"] == "nvr://ch3/x.mp4"


# -- the no-identity boundary (brief §4) -----------------------------------------------


def test_identity_event_types_are_refused() -> None:
    for t in ("FaceRecognition", "FaceDetection", "FaceCapture"):
        with pytest.raises(ValueError, match="identity"):
            normalise_nvr_event(_raw(type=t), site_id=SITE)


def test_identity_fields_in_payload_never_reach_detail() -> None:
    # Even if the raw payload smuggles identity, the allow-list keeps it out of our store.
    ev = normalise_nvr_event(_raw(name="J. Doe", person_id="p-77", face="<template>"), site_id=SITE)
    assert ev.detail is not None
    blob = repr(ev.model_dump())
    assert "J. Doe" not in blob and "p-77" not in blob and "template" not in blob
    for banned in ("name", "person_id", "face"):
        assert banned not in ev.detail


# -- loud rejection of malformed input -------------------------------------------------


def test_missing_required_field_is_rejected() -> None:
    for missing in ("id", "type", "channel", "time"):
        bad = _raw()
        del bad[missing]
        with pytest.raises(ValueError, match="missing required field"):
            normalise_nvr_event(bad, site_id=SITE)


def test_unknown_event_type_is_rejected_not_guessed() -> None:
    with pytest.raises(ValueError, match="unknown NVR event type"):
        normalise_nvr_event(_raw(type="SomeFutureThing"), site_id=SITE)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        normalise_nvr_event(_raw(time="2026-09-12T22:14:07"), site_id=SITE)


# -- ingest: persistence, zone resolution, idempotency ---------------------------------


def test_ingest_persists_one_event_per_raw_and_resolves_zone(db_session: Session) -> None:
    create_camera(db_session, site_id=SITE, name="CAM-03 Gold Room", now=_NOW, zone_id="gold-room")
    db_session.commit()

    added = ingest_nvr_events(db_session, SITE, [_raw()])
    assert len(added) == 1
    assert added[0].zone_id == "gold-room"  # resolved from the registered camera
    stored = list_events(db_session, SITE)
    assert len(stored) == 1 and stored[0].type == "zone_breach"


def test_replay_is_idempotent(db_session: Session) -> None:
    first = ingest_nvr_events(db_session, SITE, SAMPLE_EVENTS)
    assert len(first) == len(SAMPLE_EVENTS)
    # Replaying the same batch adds nothing, and duplicates within a batch collapse.
    second = ingest_nvr_events(db_session, SITE, SAMPLE_EVENTS + SAMPLE_EVENTS)
    assert second == []
    assert len(list_events(db_session, SITE)) == len(SAMPLE_EVENTS)


def test_unregistered_camera_still_ingests_without_zone(db_session: Session) -> None:
    added = ingest_nvr_events(db_session, SITE, [_raw(camera="CAM-99 Unknown")])
    assert len(added) == 1 and added[0].zone_id is None
