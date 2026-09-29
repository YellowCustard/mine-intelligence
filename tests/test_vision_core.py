"""First-party vision core (V1): rule registry, idempotent ingest, alarm promotion, API, MQTT.

The core side of Stage B (``docs/VISION_BUILD_GATE.md`` §3-§5): the edge sends thin contracts;
the core validates or rejects loudly, stores idempotently, and promotes rule conclusions into the
one alarm queue with provenance + evidence — never identity, never a fleet-asset claim, always
advisory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.auth.service import create_user
from minemonitor.cameras.service import create_camera
from minemonitor.events.repository import list_events
from minemonitor.ingest.authz import parse_vision_topic, vision_topic_matches_payload
from minemonitor.retention import run_retention
from minemonitor.storage.models import (
    AuditLog,
    Camera,
    VisionCameraHealth,
    VisionObservation,
    VisionOperationalEvent,
    VisionZoneRollup,
    Zone,
)
from minemonitor.vision import service
from minemonitor.vision.service import VisionIngestError
from tests.conftest import ADMIN, DEVICE, VIEWER, make_client

SITE = "kn-zw-01"
NODE = "edge-01"
_NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


@pytest.fixture
def cam(db_session: Session) -> Camera:
    db_session.add(
        Zone(
            zone_id="gold-room",
            site_id=SITE,
            name="Gold Room",
            kind="restricted",
            geometry={},
            rules={"max_occupancy": 2, "severity": "critical"},
        )
    )
    c = create_camera(db_session, site_id=SITE, name="CAM-GR-01", now=_NOW)
    c.image_zones = [
        {
            "zone_id": "gold-room",
            "zone_version": "v1",
            "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
        }
    ]
    service.install_default_rules(db_session, SITE, author="test", at=_NOW - timedelta(days=1))
    db_session.commit()
    return c


def _op(camera_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "vision.operational_event.v1",
        "event_id": f"vop-{camera_id}-person_in_restricted_zone-7-20260928T080000Z",
        "site_id": SITE,
        "node_id": NODE,
        "camera_id": camera_id,
        "ts": "2026-09-28T08:00:02Z",
        "type": "person_in_restricted_zone",
        "provenance": "inferred",
        "confidence": 0.81,
        "rule_id": "person_in_restricted_zone",
        "rule_version": "1.0.0",
        "subject": {"track_id": f"{camera_id}:7"},
        "zone_id": "gold-room",
        "zone_version": "v1",
        "evidence": {
            "observation_window": ["2026-09-28T08:00:00Z", "2026-09-28T08:00:02Z"],
            "track_ids": [f"{camera_id}:7"],
            "frame_ids": ["f1", "f2", "f3"],
            "signals": {"dwell_ms": 2000, "observations": 5, "min_conf": 0.72},
            "clip_ref": None,
        },
        "advisory": True,
    }
    base.update(over)
    return base


def _rollup(camera_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "vision.zone_rollup.v1",
        "rollup_id": f"vr-{camera_id}-gold-room-20260928T080000Z-person",
        "site_id": SITE,
        "node_id": NODE,
        "camera_id": camera_id,
        "zone_id": "gold-room",
        "ts": "2026-09-28T08:00:00Z",
        "bucket_s": 300,
        "object_class": "person",
        "count_max": 3,
        "count_mean": 1.4,
        "entries": 4,
        "exits": 3,
        "dwell_s_max": 140.0,
        "dwell_s_p50": 35.0,
        "observed_s": 300.0,
        "advisory": True,
    }
    base.update(over)
    return base


def _cam_health(camera_id: str, ts: str, state: str = "ok") -> dict[str, Any]:
    return {
        "schema": "vision.camera_health.v1",
        "health_id": f"vch-{camera_id}-{ts}",
        "site_id": SITE,
        "node_id": NODE,
        "camera_id": camera_id,
        "ts": ts,
        "state": state,
        "blur_score": 180.0,
        "luma_mean": 96.0,
        "frozen_frames": 0,
        "advisory": True,
    }


# -- rules ----------------------------------------------------------------------------------------


def test_default_rules_install_once(db_session: Session) -> None:
    first = service.install_default_rules(db_session, SITE, author="t", at=_NOW)
    again = service.install_default_rules(db_session, SITE, author="t", at=_NOW)
    assert {r.rule_id for r in first} >= {
        "person_in_restricted_zone",
        "person_dwell",
        "zone_occupancy_exceeded",
        "camera_obstructed",
    }
    assert again == []  # idempotent


def test_rule_versions_are_immutable(db_session: Session, cam: Camera) -> None:
    rule = service.rule_to_contract(
        service.get_rule_version(db_session, SITE, "person_dwell", "1.0.0")  # type: ignore[arg-type]
    )
    from minemonitor.contracts.vision import VisionRuleVersionV1

    with pytest.raises(VisionIngestError):
        service.create_rule_version(
            db_session, VisionRuleVersionV1.model_validate(rule), author="x"
        )


def test_retired_rule_is_not_active(db_session: Session, cam: Camera) -> None:
    service.retire_rule_version(db_session, SITE, "person_dwell", "1.0.0", at=_NOW)
    active = {r.rule_id for r in service.list_rule_versions(db_session, SITE, active_at=_NOW)}
    assert "person_dwell" not in active and "person_in_restricted_zone" in active


# -- operational events → alarms ------------------------------------------------------------------


def test_operational_event_promotes_one_alarm_and_is_idempotent(
    db_session: Session, cam: Camera
) -> None:
    rec = service.parse(_op(cam.id))
    r1 = service.ingest(db_session, rec, received_at=_NOW)
    db_session.commit()
    assert r1["created"] and r1["alarm_event_id"] == service.alarm_id_for(rec.event_id)  # type: ignore[union-attr]
    r2 = service.ingest(db_session, service.parse(_op(cam.id)), received_at=_NOW)
    db_session.commit()
    assert r2["created"] is False
    alarms = list_events(db_session, site_id=SITE)
    assert len(alarms) == 1
    a = alarms[0]
    assert a.type == "zone_breach" and a.severity == "critical"  # severity from rule policy
    assert a.source == f"vision:{cam.id}" and a.zone_id == "gold-room"
    assert a.asset_id is None  # vision never claims a fleet asset without fusion
    assert a.detail["provenance"] == "inferred" and a.detail["rule_version"] == "1.0.0"
    assert a.evidence["vision_operational_event_id"] == rec.event_id  # type: ignore[union-attr]
    assert "Gold Room" in a.summary


def test_occupancy_severity_comes_from_zone_policy(db_session: Session, cam: Camera) -> None:
    rec = service.parse(
        _op(
            cam.id,
            event_id="vop-occ-1",
            type="zone_occupancy_exceeded",
            rule_id="zone_occupancy_exceeded",
            subject=None,
            evidence={
                "observation_window": ["2026-09-28T08:00:00Z", "2026-09-28T08:00:04Z"],
                "signals": {"count": 3, "max_occupancy": 2},
            },
        )
    )
    service.ingest(db_session, rec, received_at=_NOW)
    db_session.commit()
    (a,) = list_events(db_session, site_id=SITE)
    assert a.type == "zone_occupancy"
    assert a.severity == "critical"  # the zone's cap severity, not the rule default (warning)
    assert a.detail["count"] == 3 and "exceeds capacity 2" in a.summary


def test_analytics_only_types_are_not_promoted(db_session: Session, cam: Camera) -> None:
    from minemonitor.contracts.vision import VisionRuleVersionV1

    service.create_rule_version(
        db_session,
        VisionRuleVersionV1.model_validate(
            {
                "site_id": SITE,
                "rule_id": "zone_entry",
                "rule_version": "1.0.0",
                "event_type": "zone_entry",
                "parameters": {
                    "classes": ["person"],
                    "min_observations": 2,
                    "min_dwell_ms": 0,
                    "hysteresis_observations": 2,
                    "cooldown_ms": 0,
                    "window_ms": 2000,
                    "confidence_threshold": 0.5,
                    "severity": "info",
                },
                "active_from": "2026-09-27T00:00:00Z",
            }
        ),
        author="t",
    )
    r = service.ingest(
        db_session,
        service.parse(_op(cam.id, event_id="vop-entry", type="zone_entry", rule_id="zone_entry")),
        received_at=_NOW,
    )
    db_session.commit()
    assert r["created"] and r["alarm_event_id"] is None
    assert list_events(db_session, site_id=SITE) == []


def test_unknown_rule_version_is_refused(db_session: Session, cam: Camera) -> None:
    with pytest.raises(VisionIngestError, match="unknown rule"):
        service.ingest(db_session, service.parse(_op(cam.id, rule_version="9.9.9")))


def test_rule_type_mismatch_is_refused(db_session: Session, cam: Camera) -> None:
    with pytest.raises(VisionIngestError, match="produces"):
        service.ingest(db_session, service.parse(_op(cam.id, type="person_dwell")))


def test_unregistered_camera_is_refused(db_session: Session, cam: Camera) -> None:
    with pytest.raises(VisionIngestError, match="not registered"):
        service.ingest(
            db_session,
            service.parse(
                _op(
                    "cam-ghost",
                    subject=None,
                    evidence={
                        "observation_window": ["2026-09-28T08:00:00Z", "2026-09-28T08:00:01Z"]
                    },
                )
            ),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        {"provenance": "observed"},  # a rule conclusion is never 'observed'
        {"confidence": 1.2},
        {"rule_version": "v1"},
        {"advisory": False},
        {"operator_name": "J. Moyo"},  # no identity field, ever
        {"face_template": "AAAA"},
        {"subject": {"track_id": "x:1", "asset_id": "HT-102"}},  # asset without association
        {"evidence": {"observation_window": ["2026-09-28T08:00:05Z", "2026-09-28T08:00:00Z"]}},
    ],
)
def test_malformed_operational_event_rejected_loudly(
    db_session: Session, cam: Camera, mutate: dict[str, Any]
) -> None:
    with pytest.raises(VisionIngestError):
        service.parse(_op(cam.id, **mutate))


def test_vendor_event_and_rule_contracts_not_ingestible_from_edge() -> None:
    with pytest.raises(VisionIngestError, match="not an ingestible"):
        service.parse({"schema": "vision.vendor_event.v1"})
    with pytest.raises(VisionIngestError, match="not an ingestible"):
        service.parse({"schema": "vision.rule_version.v1"})


# -- rollups, health, observations ---------------------------------------------------------------


def test_rollup_and_health_idempotent(db_session: Session, cam: Camera) -> None:
    for _ in range(2):
        service.ingest(db_session, service.parse(_rollup(cam.id)), received_at=_NOW)
        service.ingest(
            db_session, service.parse(_cam_health(cam.id, "2026-09-28T08:00:00Z")), received_at=_NOW
        )
    db_session.commit()
    assert len(db_session.execute(select(VisionZoneRollup)).scalars().all()) == 1
    assert len(db_session.execute(select(VisionCameraHealth)).scalars().all()) == 1
    db_session.refresh(cam)
    assert cam.health_state == "online"


def test_rollup_coverage_cannot_exceed_bucket(cam: Camera) -> None:
    with pytest.raises(VisionIngestError):
        service.parse(_rollup(cam.id, observed_s=301.0))


def test_observation_stored_with_model_provenance(db_session: Session, cam: Camera) -> None:
    obs = {
        "schema": "vision.observation.v1",
        "observation_id": f"vobs-{cam.id}-f1",
        "site_id": SITE,
        "node_id": NODE,
        "camera_id": cam.id,
        "ts": "2026-09-28T08:00:00Z",
        "frame_id": "f1",
        "model": {"id": "yolox-nano", "version": "0.1.0"},
        "objects": [
            {
                "track_id": f"{cam.id}:7",
                "class": "person",
                "confidence": 0.8,
                "bbox": {"x1": 0.2, "y1": 0.2, "x2": 0.3, "y2": 0.6},
                "zone_ids": ["gold-room"],
                "provenance": "observed",
            }
        ],
        "advisory": True,
    }
    assert service.ingest(db_session, service.parse(obs), received_at=_NOW)["created"]
    assert not service.ingest(db_session, service.parse(obs), received_at=_NOW)["created"]
    db_session.commit()
    row = db_session.execute(select(VisionObservation)).scalar_one()
    assert row.model_id == "yolox-nano" and row.objects[0]["provenance"] == "observed"
    bad = {**obs, "objects": [{**obs["objects"][0], "track_id": "other-cam:7"}]}
    with pytest.raises(VisionIngestError):
        service.parse(bad)  # track ids are namespaced to their camera


# -- camera_offline ("missed" lane) ---------------------------------------------------------------


def test_camera_offline_raised_once_for_silent_commissioned_camera(
    db_session: Session, cam: Camera
) -> None:
    other = create_camera(db_session, site_id=SITE, name="CAM-NEVER", now=_NOW)  # never reported
    service.ingest(
        db_session, service.parse(_cam_health(cam.id, "2026-09-28T07:50:00Z")), received_at=_NOW
    )
    db_session.commit()
    evs = service.detect_camera_offline(db_session, SITE, stale_s=300, now=_NOW)
    assert len(evs) == 1 and evs[0].type == "camera_offline"
    assert evs[0].source == f"vision:{cam.id}" and other.id not in evs[0].source
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=_NOW) == []  # deduped


def test_fresh_camera_is_not_offline(db_session: Session, cam: Camera) -> None:
    service.ingest(
        db_session, service.parse(_cam_health(cam.id, "2026-09-28T07:58:00Z")), received_at=_NOW
    )
    db_session.commit()
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=_NOW) == []


# -- edge config ------------------------------------------------------------------------------


def test_edge_config_has_zones_rules_and_no_stream_secret(db_session: Session, cam: Camera) -> None:
    cam.stream_url = "rtsp://user:secret@10.0.0.5/stream"
    db_session.commit()
    cfg = service.edge_config(db_session, SITE, at=_NOW)
    assert cfg["zones"]["gold-room"] == {
        "name": "Gold Room",
        "kind": "restricted",
        "max_occupancy": 2,
    }
    assert [c["camera_id"] for c in cfg["cameras"]] == [cam.id]
    assert "secret" not in str(cfg) and "stream_url" not in str(cfg)
    assert {r["rule_id"] for r in cfg["rules"]} >= {"person_in_restricted_zone"}


# -- API --------------------------------------------------------------------------------------


def test_api_device_ingest_batch_and_reads(db_session: Session, cam: Camera) -> None:
    dev = make_client(db_session, DEVICE)
    r = dev.post("/api/v1/ingest/vision", json=[_op(cam.id), _rollup(cam.id)])
    assert r.status_code == 202, r.text
    assert r.json()["created"] == 2 and len(r.json()["alarms"]) == 1
    again = dev.post("/api/v1/ingest/vision", json=[_op(cam.id), _rollup(cam.id)])
    assert again.json() == {"accepted": 2, "created": 0, "duplicates": 2, "alarms": []}
    viewer = make_client(db_session, VIEWER)
    ev = viewer.get(f"/api/v1/sites/{SITE}/vision/events").json()
    assert ev[0]["provenance"] == "inferred" and ev[0]["alarm_event_id"]
    ro = viewer.get(f"/api/v1/sites/{SITE}/vision/rollups", params={"zone_id": "gold-room"}).json()
    assert ro[0]["count_max"] == 3 and ro[0]["provenance"] == "observed"


def test_api_bad_record_rejects_whole_batch(db_session: Session, cam: Camera) -> None:
    dev = make_client(db_session, DEVICE)
    r = dev.post("/api/v1/ingest/vision", json=[_rollup(cam.id), _op(cam.id, confidence=7)])
    assert r.status_code == 422 and r.json()["detail"]["index"] == 1
    assert db_session.execute(select(VisionZoneRollup)).first() is None  # nothing partially stored
    r2 = dev.post(
        "/api/v1/ingest/vision", json=[_rollup(cam.id), _op(cam.id, rule_version="2.0.0")]
    )
    assert r2.status_code == 422
    assert db_session.execute(select(VisionOperationalEvent)).first() is None


def test_api_site_scoped_device_cannot_publish_other_site(db_session: Session, cam: Camera) -> None:
    create_user(
        db_session,
        username="edge-other",
        password="testpass123",
        role="device",
        site_id="other-site",
    )
    db_session.commit()
    dev = make_client(db_session, ("edge-other", "testpass123"))
    assert dev.post("/api/v1/ingest/vision", json=_op(cam.id)).status_code == 403


def test_api_viewer_cannot_ingest_or_read_config(db_session: Session, cam: Camera) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.post("/api/v1/ingest/vision", json=_op(cam.id)).status_code == 403
    assert viewer.get(f"/api/v1/sites/{SITE}/vision/config").status_code == 403
    assert (
        make_client(db_session, DEVICE).get(f"/api/v1/sites/{SITE}/vision/config").status_code
        == 200
    )


def test_api_rule_publish_is_admin_audited_and_immutable(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    assert admin.post(f"/api/v1/sites/{SITE}/vision/rules/defaults").status_code == 201
    body = {
        "schema": "vision.rule_version.v1",
        "site_id": SITE,
        "rule_id": "person_dwell",
        "rule_version": "1.1.0",
        "event_type": "person_dwell",
        "parameters": {
            "classes": ["person"],
            "min_observations": 5,
            "min_dwell_ms": 90000,
            "hysteresis_observations": 3,
            "cooldown_ms": 300000,
            "window_ms": 10000,
            "confidence_threshold": 0.5,
            "severity": "warning",
        },
        "active_from": "2026-09-28T00:00:00Z",
        "advisory": True,
    }
    assert admin.post(f"/api/v1/sites/{SITE}/vision/rules", json=body).status_code == 201
    assert admin.post(f"/api/v1/sites/{SITE}/vision/rules", json=body).status_code == 409
    viewer = make_client(db_session, VIEWER)
    assert viewer.post(f"/api/v1/sites/{SITE}/vision/rules", json=body).status_code == 403
    retire = admin.post(f"/api/v1/sites/{SITE}/vision/rules/person_dwell/1.0.0/retire")
    assert retire.status_code == 200 and retire.json()["active_to"]
    actions = set(db_session.execute(select(AuditLog.action)).scalars().all())
    assert {"vision.rule.install_defaults", "vision.rule.publish", "vision.rule.retire"} <= actions


# -- MQTT topic authz ---------------------------------------------------------------------------


def test_vision_topic_parse_and_antispoof(cam: Camera) -> None:
    assert parse_vision_topic("mm", f"mm/{SITE}/vision/{cam.id}/event") == (SITE, cam.id, "event")
    assert parse_vision_topic("mm", f"mm/{SITE}/HT-102/position") is None
    rec = service.parse(_op(cam.id))
    assert vision_topic_matches_payload("mm", f"mm/{SITE}/vision/{cam.id}/event", rec)
    assert not vision_topic_matches_payload("mm", f"mm/{SITE}/vision/other-cam/event", rec)
    assert not vision_topic_matches_payload("mm", f"mm/other/vision/{cam.id}/event", rec)
    node = service.parse(
        {
            "schema": "vision.health.v1",
            "health_id": "vh-1",
            "site_id": SITE,
            "node_id": NODE,
            "ts": "2026-09-28T08:00:00Z",
            "status": "ok",
            "cameras_active": 1,
            "fps_mean": 4.0,
            "queue_depth": 0,
            "dropped_frames": 0,
            "advisory": True,
        }
    )
    assert vision_topic_matches_payload("mm", f"mm/{SITE}/vision/{NODE}/health", node)
    assert not vision_topic_matches_payload("mm", f"mm/{SITE}/vision/edge-99/health", node)


# -- retention ------------------------------------------------------------------------------


def test_retention_purges_vision_classes(db_session: Session, cam: Camera) -> None:
    old = "2026-06-01T08:00:00Z"
    service.ingest(db_session, service.parse(_cam_health(cam.id, old)), received_at=_NOW)
    service.ingest(db_session, service.parse(_rollup(cam.id, ts=old)), received_at=_NOW)
    db_session.commit()
    deleted = run_retention(
        db_session,
        now=_NOW,
        positions_days=0,
        metrics_days=30,
        events_days=0,
        vision_health_days=30,
    )
    assert deleted["vision_camera_health"] == 1 and deleted["vision_zone_rollups"] == 1
