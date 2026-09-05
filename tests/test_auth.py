"""Auth, role hierarchy and per-site scoping (M6)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from minemonitor.auth.hashing import hash_password, verify_password
from minemonitor.auth.service import create_user
from tests.conftest import ADMIN, DEVICE, SUPERVISOR, VIEWER, make_client

_POS = {
    "schema": "asset.position.v1",
    "site_id": "kn-zw-01",
    "asset_id": "HT-102",
    "ts": "2026-09-05T11:42:07Z",
    "lat": -17.8252,
    "lon": 31.0335,
    "source": "test",
}


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)
    assert h != hash_password("correct horse battery staple")  # salted


def test_unauthenticated_is_rejected(db_session: Session) -> None:
    anon = make_client(db_session, None)
    r = anon.get("/sites/kn-zw-01/state")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_bad_credentials_rejected(db_session: Session) -> None:
    bad = make_client(db_session, ("admin", "nope"))
    assert bad.get("/sites/kn-zw-01/state").status_code == 401


def test_viewer_can_read_not_write(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    assert c.get("/sites/kn-zw-01/state").status_code == 200
    # ack requires supervisor
    assert c.post("/sites/kn-zw-01/events/x/ack", json={}).status_code == 403
    # zone create requires admin
    zone = {"zone_id": "z", "name": "Z", "kind": "generic", "geometry": {}, "rules": {}}
    assert c.post("/sites/kn-zw-01/zones", json=zone).status_code == 403


def test_supervisor_can_ack(db_session: Session) -> None:
    c = make_client(db_session, SUPERVISOR)
    # No such event -> 404 (but crucially NOT 403, so the role gate passed).
    assert c.post("/sites/kn-zw-01/events/none/ack", json={}).status_code == 404


def test_device_ingest_gate(db_session: Session) -> None:
    device = make_client(db_session, DEVICE)
    assert device.post("/ingest/positions", json=_POS).status_code == 202
    viewer = make_client(db_session, VIEWER)
    assert viewer.post("/ingest/positions", json=_POS).status_code == 403


def test_admin_can_write(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    zone = {
        "zone_id": "r1",
        "name": "Mag",
        "kind": "restricted",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
        "rules": {"authorized_classes": []},
    }
    assert c.post("/sites/kn-zw-01/zones", json=zone).status_code == 201


def test_site_scoped_user_cannot_cross_sites(db_session: Session) -> None:
    create_user(
        db_session,
        username="other-viewer",
        password="testpass123",
        role="viewer",
        site_id="other-site",
    )
    db_session.commit()
    c = make_client(db_session, ("other-viewer", "testpass123"))
    assert c.get("/sites/other-site/state").status_code == 200
    assert c.get("/sites/kn-zw-01/state").status_code == 403
