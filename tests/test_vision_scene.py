"""Vision V4: scene configuration, live scene state, window reports, provenance in /state."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.cameras.service import create_camera
from minemonitor.storage.models import AuditLog, Camera, Zone
from minemonitor.vision import scene, service
from minemonitor.vision.sim import SIM_IMAGE_ZONES, START
from tests.conftest import ADMIN, DEVICE, VIEWER, make_client
from tests.test_vision_pipeline import _FAR_POLYGON, run_scene

SITE = "kn-zw-01"
NODE = "edge-01"
T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
SQUARE = [[0.5, 0.4], [0.95, 0.4], [0.95, 0.95], [0.5, 0.95]]


@pytest.fixture
def cam(db_session: Session) -> Camera:
    db_session.add_all(
        [
            Zone(
                zone_id="gr",
                site_id=SITE,
                name="Gold Room",
                kind="restricted",
                geometry=_FAR_POLYGON,
                rules={},
            ),
            Zone(
                zone_id="yard",
                site_id=SITE,
                name="Yard",
                kind="generic",
                geometry=_FAR_POLYGON,
                rules={"max_occupancy": 2},
            ),
        ]
    )
    c = create_camera(db_session, site_id=SITE, name="CAM-GR-01", now=T0)
    service.install_default_rules(db_session, SITE, author="t", at=T0 - timedelta(days=1))
    db_session.commit()
    return c


def _scene(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "image_zones": [{"zone_id": "gr", "zone_version": "2026-09-28", "polygon": SQUARE}]
    }
    body.update(over)
    return body


# -- scene configuration --------------------------------------------------------------------


def test_put_scene_admin_audited_and_feeds_edge_config(db_session: Session, cam: Camera) -> None:
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=_scene())
    assert r.status_code == 200, r.text
    assert r.json()["calibration_status"] == "none" and r.json()["homography"] is None
    cfg = service.edge_config(db_session, SITE, at=T0)
    (c,) = cfg["cameras"]
    assert c["image_zones"][0]["zone_id"] == "gr" and cfg["zones"]["gr"]["kind"] == "restricted"
    actions = set(db_session.execute(select(AuditLog.action)).scalars())
    assert "vision.scene.update" in actions
    assert (
        make_client(db_session, VIEWER)
        .get(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene")
        .json()["image_zones"][0]["zone_version"]
        == "2026-09-28"
    )


def test_homography_sets_calibrated_and_null_clears(db_session: Session, cam: Camera) -> None:
    admin = make_client(db_session, ADMIN)
    h = {"matrix": [[0.001, 0, -17.83], [0, 0.001, 31.03], [0, 0, 1]]}
    r = admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=_scene(homography=h))
    assert r.json()["calibration_status"] == "calibrated"
    cfg = service.edge_config(db_session, SITE, at=T0)
    assert cfg["cameras"][0]["homography"]["matrix"][0][2] == -17.83
    # omitting homography leaves calibration alone …
    r2 = admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=_scene())
    assert r2.json()["calibration_status"] == "calibrated"
    # … explicit null clears it
    r3 = admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=_scene(homography=None))
    assert r3.json()["calibration_status"] == "none" and r3.json()["homography"] is None


@pytest.mark.parametrize(
    "zones",
    [
        [{"zone_id": "nope", "zone_version": "v1", "polygon": SQUARE}],  # unknown zone
        [{"zone_id": "gr", "zone_version": "v1", "polygon": [[0, 0], [1.2, 0], [1, 1]]}],
        [{"zone_id": "gr", "zone_version": "v1", "polygon": [[0, 0], [1, 1]]}],  # < 3 points
        [{"zone_id": "gr", "polygon": SQUARE}],  # no zone_version
        [{"zone_id": "gr", "zone_version": "v1", "polygon": SQUARE}] * 2,  # duplicate
        [{"zone_id": "gr", "zone_version": "v1", "polygon": SQUARE}] * 17,  # too many
    ],
)
def test_bad_scene_rejected(db_session: Session, cam: Camera, zones: list[Any]) -> None:
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json={"image_zones": zones})
    assert r.status_code == 422, r.text
    db_session.refresh(cam)
    assert not cam.image_zones  # nothing half-applied


def test_bad_homography_and_access_rules(db_session: Session, cam: Camera) -> None:
    admin = make_client(db_session, ADMIN)
    bad = _scene(homography={"matrix": [[1, 0], [0, 1]]})
    assert admin.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=bad).status_code == 422
    viewer = make_client(db_session, VIEWER)
    assert (
        viewer.put(f"/api/v1/sites/{SITE}/cameras/{cam.id}/scene", json=_scene()).status_code == 403
    )
    assert admin.put(f"/api/v1/sites/{SITE}/cameras/ghost/scene", json=_scene()).status_code == 404


# -- live scene state --------------------------------------------------------------------------


def _ingest(db: Session, payloads: list[dict[str, Any]]) -> None:
    r = make_client(db, DEVICE).post("/api/v1/ingest/vision", json=payloads)
    assert r.status_code == 202, r.text


def _rollup(cam_id: str, zone: str | None, ts: datetime, **over: Any) -> dict[str, Any]:
    body = {
        "schema": "vision.zone_rollup.v1",
        "rollup_id": f"vr-{cam_id}-{zone or 'view'}-{ts:%Y%m%dT%H%M%S}",
        "site_id": SITE,
        "node_id": NODE,
        "camera_id": cam_id,
        "zone_id": zone,
        "ts": ts.isoformat(),
        "bucket_s": 300,
        "object_class": "person",
        "count_max": 2,
        "count_mean": 1.0,
        "entries": 2,
        "exits": 1,
        "dwell_s_max": 40.0,
        "dwell_s_p50": 20.0,
        "observed_s": 300.0,
        "advisory": True,
    }
    body.update(over)
    return body


def test_scene_state_counts_condition_and_staleness(db_session: Session, cam: Camera) -> None:
    cam.image_zones = [{"zone_id": "gr", "zone_version": "v1", "polygon": SQUARE}]
    db_session.commit()
    _ingest(
        db_session,
        [
            _rollup(cam.id, None, T0, count_max=3),
            _rollup(cam.id, "gr", T0, count_max=2),
            {
                "schema": "vision.camera_health.v1",
                "health_id": "h1",
                "site_id": SITE,
                "node_id": NODE,
                "camera_id": cam.id,
                "ts": (T0 + timedelta(minutes=4)).isoformat(),
                "state": "dark",
                "advisory": True,
            },
        ],
    )
    fresh = scene.scene_state(db_session, SITE, now=T0 + timedelta(minutes=6))
    (c,) = fresh["cameras"]
    assert c["people_in_view_peak"] == 3 and c["zones"][0]["people_peak"] == 2
    assert c["condition"] == "dark" and c["stale"] is False and c["provenance"] == "observed"
    later = scene.scene_state(db_session, SITE, now=T0 + timedelta(hours=1))
    assert later["cameras"][0]["stale"] is True  # old rollup is labelled, not shown as live


def test_scene_state_omits_unconfigured_cameras(db_session: Session, cam: Camera) -> None:
    assert scene.scene_state(db_session, SITE)["cameras"] == []
    viewer = make_client(db_session, VIEWER)
    assert viewer.get(f"/api/v1/sites/{SITE}/vision/scene").json()["cameras"] == []


# -- reporting -------------------------------------------------------------------------------


def test_report_aggregates_peak_weighted_mean_and_coverage(
    db_session: Session, cam: Camera
) -> None:
    _ingest(
        db_session,
        [
            _rollup(cam.id, None, T0, count_max=4, count_mean=2.0, observed_s=300.0),
            _rollup(
                cam.id,
                None,
                T0 + timedelta(minutes=5),
                count_max=1,
                count_mean=0.5,
                observed_s=100.0,
                entries=1,
                exits=1,
            ),
            _rollup(cam.id, "gr", T0, count_max=2, count_mean=1.0, observed_s=300.0),
        ],
    )
    rep = scene.vision_report(db_session, SITE, T0, T0 + timedelta(minutes=20))
    view = next(r for r in rep["rows"] if r["zone_id"] is None)
    assert view["people_peak"] == 4
    assert view["people_mean"] == pytest.approx((2.0 * 300 + 0.5 * 100) / 400)  # time-weighted
    assert view["entries"] == 3 and view["coverage_pct"] == pytest.approx(33.3)  # 400 s of 1200 s
    gr = next(r for r in rep["rows"] if r["zone_id"] == "gr")
    assert gr["zone_name"] == "Gold Room" and gr["coverage_pct"] == view["coverage_pct"]
    with pytest.raises(ValueError):
        scene.vision_report(db_session, SITE, T0, T0)


def test_report_api_window_csv_and_default_shift(db_session: Session, cam: Camera) -> None:
    _ingest(db_session, [_rollup(cam.id, "gr", T0, count_max=5)])
    viewer = make_client(db_session, VIEWER)
    params = {"since": T0.isoformat(), "until": (T0 + timedelta(hours=1)).isoformat()}
    j = viewer.get(f"/api/v1/sites/{SITE}/vision/report", params=params).json()
    assert j["rows"][0]["people_peak"] == 5 and j["provenance"] == "observed"
    r = viewer.get(f"/api/v1/sites/{SITE}/vision/report.csv", params=params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["zone_id"] == "gr" and rows[0]["people_peak"] == "5"
    assert viewer.get(f"/api/v1/sites/{SITE}/vision/report").status_code == 200  # current shift
    bad = viewer.get(f"/api/v1/sites/{SITE}/vision/report", params={"since": T0.isoformat()})
    assert bad.status_code == 400


def test_report_counts_restricted_intrusions_end_to_end(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    """Simulated intruder → one alarm → the report counts one intrusion for that zone."""
    db_session.add(
        Zone(
            zone_id="sim-restricted",
            site_id=SITE,
            name="Sim Restricted",
            kind="restricted",
            geometry=_FAR_POLYGON,
            rules={},
        )
    )
    db_session.add(
        Zone(
            zone_id="sim-yard",
            site_id=SITE,
            name="Sim Yard",
            kind="generic",
            geometry=_FAR_POLYGON,
            rules={"max_occupancy": 2},
        )
    )
    cam.image_zones = SIM_IMAGE_ZONES
    db_session.commit()
    run_scene(db_session, cam, "intruder", tmp_path)
    rep = scene.vision_report(
        db_session, SITE, START - timedelta(minutes=5), START + timedelta(minutes=10)
    )
    row = next(r for r in rep["rows"] if r["zone_id"] == "sim-restricted")
    assert row["restricted_intrusions"] == 1 and row["people_peak"] == 1
    view = next(r for r in rep["rows"] if r["zone_id"] is None)
    assert view["restricted_intrusions"] == 1


# -- provenance travels to the dashboard ------------------------------------------------------


def test_state_snapshot_carries_alarm_provenance(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    db_session.add(
        Zone(
            zone_id="sim-restricted",
            site_id=SITE,
            name="Sim Restricted",
            kind="restricted",
            geometry=_FAR_POLYGON,
            rules={},
        )
    )
    db_session.add(
        Zone(
            zone_id="sim-yard",
            site_id=SITE,
            name="Sim Yard",
            kind="generic",
            geometry=_FAR_POLYGON,
            rules={},
        )
    )
    cam.image_zones = SIM_IMAGE_ZONES
    db_session.commit()
    run_scene(db_session, cam, "intruder", tmp_path)
    snap = make_client(db_session, VIEWER).get(f"/api/v1/sites/{SITE}/state").json()
    (ev,) = [e for e in snap["events"] if e["source"].startswith("vision:")]
    assert ev["provenance"] == "inferred"
