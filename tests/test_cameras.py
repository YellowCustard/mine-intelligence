"""Camera estate & AI-readiness (Mine Monitor Vision, FP-01 / RAN Mines Slice 1).

Registry/config only: CRUD, AI-readiness assessment, estate rollup, RBAC, /api/v1 mount,
site scoping, and the invariant that ``stream_url`` (a secret) is never returned.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

_CAM = {
    "name": "Gold Room North",
    "location_description": "gold room, north corner",
    "make_model": "Hikvision DS-2CD",
    "stream_url": "rtsp://user:pass@10.0.0.7/Streaming/Channels/102",
    "stream_type": "rtsp",
    "stream_kind": "secondary",
}


def test_create_read_and_stream_url_is_never_returned(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    r = admin.post("/sites/kn-zw-01/cameras", json=_CAM)
    assert r.status_code == 201
    body = r.json()
    cid = body["id"]
    # Defaults applied; the secret is never echoed back, only its presence.
    assert body["ai_suitability"] == "unknown" and body["health_state"] == "unknown"
    assert body["has_stream_url"] is True
    assert "stream_url" not in body
    # Read back, single and list — still no secret.
    one = admin.get(f"/sites/kn-zw-01/cameras/{cid}").json()
    assert "stream_url" not in one and one["stream_kind"] == "secondary"
    listing = admin.get("/sites/kn-zw-01/cameras").json()
    assert len(listing) == 1 and "stream_url" not in listing[0]


def test_assessment_records_ai_readiness(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    cid = admin.post("/sites/kn-zw-01/cameras", json={"name": "Pit Cam 1"}).json()["id"]
    r = admin.post(
        f"/sites/kn-zw-01/cameras/{cid}/assessment",
        json={
            "has_usable_ai_stream": True,
            "ai_suitability": "suitable",
            "resolution": "1920x1080",
            "fps": 12,
            "codec": "h264",
            "lighting": "mixed",
            "blind_spot_notes": "pillar occludes SE corner",
        },
    )
    assert r.status_code == 200
    c = r.json()
    assert c["has_usable_ai_stream"] is True and c["ai_suitability"] == "suitable"
    assert c["fps"] == 12 and c["lighting"] == "mixed"


def test_readiness_summary_rolls_up_the_estate(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    admin.post(
        "/sites/kn-zw-01/cameras",
        json={"name": "A", "ai_suitability": "suitable", "has_usable_ai_stream": True},
    )
    admin.post("/sites/kn-zw-01/cameras", json={"name": "B", "ai_suitability": "unsuitable"})
    admin.post("/sites/kn-zw-01/cameras", json={"name": "C"})  # unknown
    s = admin.get("/sites/kn-zw-01/cameras/readiness").json()
    assert s["total"] == 3
    assert s["ai_usable_stream"] == 1
    assert s["by_ai_suitability"] == {"suitable": 1, "unsuitable": 1, "unknown": 1}


def test_update_is_partial_and_validates_enums(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    cid = admin.post("/sites/kn-zw-01/cameras", json={"name": "Cam", "note": "keep"}).json()["id"]
    # Partial update leaves untouched fields alone.
    r = admin.patch(f"/sites/kn-zw-01/cameras/{cid}", json={"health_state": "online"})
    assert r.status_code == 200 and r.json()["health_state"] == "online"
    assert r.json()["note"] == "keep"
    # A bad enum value is rejected loudly (400), not silently stored.
    bad = admin.patch(f"/sites/kn-zw-01/cameras/{cid}", json={"ai_suitability": "great"})
    assert bad.status_code == 400
    assert admin.get(f"/sites/kn-zw-01/cameras/{cid}").json()["ai_suitability"] == "unknown"


def test_missing_camera_is_404(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    assert admin.get("/sites/kn-zw-01/cameras/nope").status_code == 404
    assert admin.patch("/sites/kn-zw-01/cameras/nope", json={"note": "x"}).status_code == 404
    assert (
        admin.post(
            "/sites/kn-zw-01/cameras/nope/assessment", json={"ai_suitability": "suitable"}
        ).status_code
        == 404
    )


def test_rbac_and_v1_mount(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    supervisor = make_client(db_session, SUPERVISOR)
    # Viewer and supervisor cannot write (admin-only reference data)...
    assert viewer.post("/sites/kn-zw-01/cameras", json={"name": "X"}).status_code == 403
    assert supervisor.post("/sites/kn-zw-01/cameras", json={"name": "X"}).status_code == 403
    # ...but can read, including under the /api/v1 mount.
    assert viewer.get("/sites/kn-zw-01/cameras").status_code == 200
    assert viewer.get("/api/v1/sites/kn-zw-01/cameras/readiness").status_code == 200


def test_site_scoping(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    cid = admin.post("/sites/kn-zw-01/cameras", json={"name": "Scoped"}).json()["id"]
    # A different site cannot see or reach the camera.
    assert admin.get(f"/sites/other-site/cameras/{cid}").status_code == 404
    assert admin.get("/sites/other-site/cameras").json() == []


def test_create_is_audited(db_session: Session) -> None:
    make_client(db_session, ADMIN).post("/sites/kn-zw-01/cameras", json={"name": "Audited"})
    audit = make_client(db_session, ADMIN).get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "camera.create" for a in audit)
