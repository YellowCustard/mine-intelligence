"""Stage B acceptance: the full vision pipeline on synthetic scenes (``VISION_BUILD_GATE.md`` §9).

Edge runtime (simulated model → tracker → image-zones → temporal rules → rollups → crash-safe
outbox) → HTTP ingest as a device → core → the one alarm queue. No camera, no GPU, no weights.

The seven §9 criteria, each a named test:
1. a person dwelling in a restricted zone raises exactly one alarm            → test_c1_*
2. a boundary-hugging or single-frame blip raises none                       → test_c2_*
3. observations carry model id/version + ``observed`` and replay is idempotent → test_c3_*
4. conclusions are recomputable under a new rule version, old ones preserved  → test_c4_*
5. the core runs identically with the edge absent                            → test_c5_*
6. malformed records are rejected loudly (and never block the queue)         → test_c6_*
7. no identity, biometric field or frame bytes ever cross to the core         → test_c7_*
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import urllib.error
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.cameras.service import create_camera
from minemonitor.events.repository import list_events
from minemonitor.storage.models import Camera, VisionObservation, VisionOperationalEvent, Zone
from minemonitor.vision import service
from minemonitor.vision.edge.outbox import Outbox
from minemonitor.vision.edge.publish import (
    HttpPublisher,
    RejectedRecordError,
    TransientPublishError,
    drain,
)
from minemonitor.vision.edge.runtime import EdgeRuntime, FrameIn
from minemonitor.vision.sim import SIM_IMAGE_ZONES, START, SimModel, frames
from minemonitor.vision.tracker import Tracker
from tests.conftest import ADMIN, DEVICE, make_client

SITE = "kn-zw-01"
NODE = "edge-sim-01"
# Real zones always carry GeoJSON (the zones API validates it); place these away from test fixes.
_FAR_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [[30.0, -18.9], [30.01, -18.9], [30.01, -18.89], [30.0, -18.89], [30.0, -18.9]]
    ],
}


@pytest.fixture
def cam(db_session: Session) -> Camera:
    return make_sim_camera(db_session)


def make_sim_camera(db_session: Session) -> Camera:
    """The simulated camera: restricted + yard zones, its scene, and the default rules."""
    db_session.add_all(
        [
            Zone(
                zone_id="sim-restricted",
                site_id=SITE,
                name="Sim Restricted",
                kind="restricted",
                geometry=_FAR_POLYGON,
                rules={},
            ),
            Zone(
                zone_id="sim-yard",
                site_id=SITE,
                name="Sim Yard",
                kind="generic",
                geometry=_FAR_POLYGON,
                rules={"max_occupancy": 2},
            ),
        ]
    )
    c = create_camera(db_session, site_id=SITE, name="CAM-SIM-01", now=START)
    c.image_zones = SIM_IMAGE_ZONES
    service.install_default_rules(db_session, SITE, author="test", at=START - timedelta(days=1))
    db_session.commit()
    return c


class Published:
    """A publisher into the core via a device TestClient, recording every payload it sends."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.payloads: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    def __call__(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        r = self.client.post("/api/v1/ingest/vision", json=payloads)
        if r.status_code == 422:
            d = r.json()["detail"]
            raise RejectedRecordError(int(d["index"]), str(d["error"]))
        if r.status_code >= 400:
            raise TransientPublishError(f"HTTP {r.status_code}")
        self.payloads.extend(payloads)
        self.responses.append(r.json())
        return dict(r.json())


def run_scene(
    db: Session,
    cam: Camera,
    name: str,
    tmp_path: Path,
    *,
    lab: bool = False,
    publisher: Published | None = None,
    outbox_name: str = "outbox.sqlite",
) -> tuple[EdgeRuntime, Outbox, Published]:
    cfg = service.edge_config(db, SITE, at=START)
    ob = Outbox(tmp_path / outbox_name)
    rt = EdgeRuntime(
        site_id=SITE,
        node_id=NODE,
        camera_id=cam.id,
        config=cfg,
        model=SimModel(),
        outbox=ob,
        lab_mode=lab,
    )
    fs = frames(name)
    for f in fs:
        rt.process(FrameIn(f.ts, f.frame_id, f, f.camera_state))
    rt.close(fs[-1].ts + timedelta(seconds=1))
    pub = publisher or Published(make_client(db, DEVICE))
    drain(ob, pub)
    return rt, ob, pub


def alarms(db: Session) -> list[Any]:
    return list_events(db, site_id=SITE)


# -- criterion 1: exactly one alarm --------------------------------------------------------------


def test_c1_intruder_raises_exactly_one_critical_alarm(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "intruder", tmp_path)
    (a,) = alarms(db_session)
    assert a.type == "zone_breach" and a.severity == "critical"
    assert a.source == f"vision:{cam.id}" and a.zone_id == "sim-restricted"
    assert a.detail["provenance"] == "inferred" and a.detail["operational_type"] == (
        "person_in_restricted_zone"
    )
    op = db_session.get(VisionOperationalEvent, a.evidence["vision_operational_event_id"])
    assert op is not None
    assert op.evidence["signals"]["dwell_ms"] >= 1500
    assert op.evidence["signals"]["observations"] >= 3
    assert op.evidence["track_ids"] == [f"{cam.id}:1"] and op.evidence["frame_ids"]


def test_c1_occluded_intruder_still_one_alarm(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "occluded_intruder", tmp_path)
    assert [a.type for a in alarms(db_session)] == ["zone_breach"]


def test_c1_loiterer_raises_one_dwell_alarm(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "loiterer", tmp_path)
    (a,) = alarms(db_session)
    assert a.type == "zone_dwell" and a.severity == "warning" and a.zone_id == "sim-yard"
    assert a.detail["dwell_ms"] >= 120_000


def test_c1_crowd_over_capacity_one_occupancy_alarm(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "crowd", tmp_path)
    (a,) = alarms(db_session)
    assert a.type == "zone_occupancy" and a.zone_id == "sim-yard"
    assert a.detail["count"] == 3 and a.detail["max_occupancy"] == 2
    assert "3 people in Sim Yard exceeds capacity 2" in a.summary


def test_c1_dark_camera_one_obstructed_alarm(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "dark", tmp_path)
    (a,) = alarms(db_session)
    assert a.type == "camera_obstructed" and a.severity == "warning"
    db_session.refresh(cam)
    assert cam.health_state in ("online", "degraded")  # recovers to ok before the scene ends


# -- criterion 2: no false alarms -----------------------------------------------------------------


@pytest.mark.parametrize("scene", ["hugger", "blink", "crossing"])
def test_c2_no_alarm_for_boundary_hugger_blink_or_passers_by(
    db_session: Session, cam: Camera, tmp_path: Path, scene: str
) -> None:
    run_scene(db_session, cam, scene, tmp_path)
    assert alarms(db_session) == []


# -- criterion 3: provenance + idempotent replay ---------------------------------------------------


def test_c3_observations_carry_model_provenance_and_replay_is_idempotent(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "intruder", tmp_path, lab=True)
    rows = db_session.execute(select(VisionObservation)).scalars().all()
    assert len(rows) == len(frames("intruder"))
    assert {(r.model_id, r.model_version) for r in rows} == {("stub-sim", "0.1.0")}
    assert all(o["provenance"] == "observed" for r in rows for o in r.objects)
    # replay the same scene from a fresh edge (a crash that lost the outbox): nothing new
    _, _, pub = run_scene(db_session, cam, "intruder", tmp_path, lab=True, outbox_name="b.sqlite")
    assert sum(r["created"] for r in pub.responses) == 0
    assert len(alarms(db_session)) == 1


# -- criterion 4: recompute under a new rule version -----------------------------------------------


def test_c4_recompute_under_new_rule_version_keeps_old(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    run_scene(db_session, cam, "intruder", tmp_path, lab=True)
    admin = make_client(db_session, ADMIN)
    old = service.rule_to_contract(
        service.get_rule_version(db_session, SITE, "person_in_restricted_zone", "1.0.0")  # type: ignore[arg-type]
    )
    new = {
        **old,
        "rule_version": "1.1.0",
        "active_from": "2026-09-28T00:00:00Z",
        "parameters": {**old["parameters"], "min_dwell_ms": 8000},
    }
    assert admin.post(f"/api/v1/sites/{SITE}/vision/rules", json=new).status_code == 201
    r = admin.post(
        f"/api/v1/sites/{SITE}/vision/recompute",
        json={
            "camera_id": cam.id,
            "since": "2026-09-28T07:59:00Z",
            "until": "2026-09-28T08:01:00Z",
            "rule_id": "person_in_restricted_zone",
            "rule_version": "1.1.0",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["observations"] == len(frames("intruder")) and len(r.json()["created"]) == 1
    ops = db_session.execute(select(VisionOperationalEvent)).scalars().all()
    by_version = {o.rule_version: o for o in ops}
    assert set(by_version) == {"1.0.0", "1.1.0"}  # old conclusion preserved beside the new
    assert by_version["1.1.0"].evidence["signals"]["dwell_ms"] >= 8000
    assert by_version["1.1.0"].promoted_event_id is None  # recompute never raises live alarms
    assert len(alarms(db_session)) == 1
    # recomputing again under the same version is idempotent
    again = admin.post(
        f"/api/v1/sites/{SITE}/vision/recompute",
        json={
            "camera_id": cam.id,
            "since": "2026-09-28T07:59:00Z",
            "until": "2026-09-28T08:01:00Z",
            "rule_id": "person_in_restricted_zone",
            "rule_version": "1.1.0",
        },
    )
    assert again.json()["created"] == []


# -- criterion 5: core unaffected by the edge's absence -------------------------------------------


def test_c5_core_runs_identically_without_the_edge(db_session: Session, cam: Camera) -> None:
    client = make_client(db_session, ADMIN)
    r = client.post(
        "/api/v1/ingest/positions",
        json={
            "schema": "asset.position.v1",
            "site_id": SITE,
            "asset_id": "HT-102",
            "ts": "2026-09-28T08:00:00Z",
            "lat": -17.8252,
            "lon": 31.0335,
            "speed_kph": 20.0,
            "source": "test",
        },
    )
    assert r.status_code == 202, r.text
    assert client.get(f"/api/v1/sites/{SITE}/vision/events").json() == []
    assert client.get(f"/api/v1/sites/{SITE}/vision/health").json() == {"nodes": [], "cameras": []}
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=START) == []


# -- criterion 6: malformed rejected, never blocking ----------------------------------------------


def test_c6_rejected_record_is_dead_lettered_and_rest_delivered(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    ob = Outbox(tmp_path / "o.sqlite")
    good = service.parse(
        {
            "schema": "vision.health.v1",
            "health_id": "vh-good",
            "site_id": SITE,
            "node_id": NODE,
            "ts": "2026-09-28T08:00:00Z",
            "status": "ok",
            "cameras_active": 1,
            "fps_mean": 5.0,
            "queue_depth": 0,
            "dropped_frames": 0,
            "advisory": True,
        }
    )
    ob.put(good)
    # a record that was valid on the edge but the core refuses (camera not registered here)
    stranger = service.parse(
        {
            "schema": "vision.camera_health.v1",
            "health_id": "vch-ghost",
            "site_id": SITE,
            "node_id": NODE,
            "camera_id": "cam-ghost",
            "ts": "2026-09-28T08:00:01Z",
            "state": "ok",
            "advisory": True,
        }
    )
    ob.put(stranger)
    db = sqlite3.connect(tmp_path / "o.sqlite")
    db.execute(  # corrupt a queued payload to simulate on-disk damage
        "INSERT INTO outbox (id, schema, payload, created_at) VALUES ('bad', 'x', ?, 'z')",
        (json.dumps({"schema": "vision.health.v1", "health_id": "bad", "confidence": 7}),),
    )
    db.commit()
    db.close()
    stats = drain(ob, Published(make_client(db_session, DEVICE)))
    assert stats["sent"] == 1 and stats["dead"] == 2 and stats["left_pending"] == 0


def test_c6_outbox_survives_outage_and_crash(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    def down(_: list[dict[str, Any]]) -> dict[str, Any]:
        raise TransientPublishError("link down")

    rt, ob, _ = run_scene(db_session, cam, "intruder", tmp_path, publisher=down)  # type: ignore[arg-type]
    pending = ob.counts()["pending"]
    assert pending > 0 and alarms(db_session) == []
    ob.close()  # power loss
    reopened = Outbox(tmp_path / "outbox.sqlite")
    assert reopened.counts()["pending"] == pending
    stats = drain(reopened, Published(make_client(db_session, DEVICE)))
    assert stats["sent"] == pending and stats["left_pending"] == 0
    assert len(alarms(db_session)) == 1  # delivered late, exactly once


# -- criterion 7: nothing identifying crosses -----------------------------------------------------

_FORBIDDEN_KEY = re.compile(
    r"face|name|embedding|template|biometric|operator|person_id|jpeg|"
    r"png|image_bytes|frame_data|pixels",
    re.I,
)


def _walk(obj: Any, path: str = "") -> list[str]:
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _FORBIDDEN_KEY.search(k):
                bad.append(f"{path}.{k}")
            bad.extend(_walk(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad.extend(_walk(v, f"{path}[{i}]"))
    elif isinstance(obj, str) and len(obj) > 512:
        bad.append(f"{path} (suspiciously long string)")
    return bad


@pytest.mark.parametrize("scene", ["intruder", "crowd", "dark"])
def test_c7_no_identity_or_frames_cross_to_core(
    db_session: Session, cam: Camera, tmp_path: Path, scene: str
) -> None:
    _, _, pub = run_scene(db_session, cam, scene, tmp_path, lab=True)
    assert pub.payloads
    schemas = Counter(p["schema"] for p in pub.payloads)
    assert set(schemas) <= set(service.INGESTIBLE)
    for p in pub.payloads:
        assert p["advisory"] is True
        assert _walk(p) == [], p["schema"]


# -- tracking, rollups, publisher -----------------------------------------------------------------


def test_tracker_ids_stable_through_crossing() -> None:
    fs = frames("crossing")
    tracker = Tracker("cam-x")
    seen: dict[str, list[str]] = {"a": [], "b": []}
    for f in fs:
        out = tracker.update(list(f.detections), f.ts)
        by_box = {o.bbox: o.track_id for o in out}
        for det, actor in zip(f.detections, f.truth, strict=True):
            if det.bbox in by_box:
                seen[actor].append(by_box[det.bbox])
    for actor, ids in seen.items():
        majority = Counter(ids).most_common(1)[0][1]
        assert majority / len(ids) >= 0.9, (actor, Counter(ids))
    assert Counter(seen["a"]).most_common(1)[0][0] != Counter(seen["b"]).most_common(1)[0][0]


def test_tracker_drops_single_frame_blip() -> None:
    fs = frames("blink")
    tracker = Tracker("cam-x")
    assert all(tracker.update(list(f.detections), f.ts) == [] for f in fs)


def test_rollups_count_people_per_zone(db_session: Session, cam: Camera, tmp_path: Path) -> None:
    run_scene(db_session, cam, "crowd", tmp_path)
    viewer = make_client(db_session, ADMIN)
    yard = viewer.get(f"/api/v1/sites/{SITE}/vision/rollups", params={"zone_id": "sim-yard"}).json()
    (r,) = yard
    assert r["count_max"] == 3 and r["entries"] == 3
    assert 12.0 <= r["observed_s"] <= 14.0  # coverage ≈ scene length, not the 300 s bucket
    view = [
        x for x in viewer.get(f"/api/v1/sites/{SITE}/vision/rollups").json() if x["zone_id"] is None
    ]
    assert view[0]["count_max"] == 3


def test_http_publisher_maps_422_and_outages() -> None:
    pub = HttpPublisher("http://core.invalid", "edge", "pw")
    body = json.dumps({"detail": {"index": 2, "error": "bad"}}).encode()
    err = urllib.error.HTTPError(pub.url, 422, "Unprocessable", {}, io.BytesIO(body))  # type: ignore[arg-type]
    with (
        mock.patch("urllib.request.urlopen", side_effect=err),
        pytest.raises(RejectedRecordError) as exc,
    ):
        pub([{}])
    assert exc.value.index == 2
    with (
        mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")),
        pytest.raises(TransientPublishError),
    ):
        pub([{}])
    e503 = urllib.error.HTTPError(pub.url, 503, "down", {}, io.BytesIO(b""))  # type: ignore[arg-type]
    with (
        mock.patch("urllib.request.urlopen", side_effect=e503),
        pytest.raises(TransientPublishError),
    ):
        pub([{}])
