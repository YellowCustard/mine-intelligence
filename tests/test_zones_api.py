"""HTTP coverage for zone CRUD read/update/delete and their gates (Phase 3)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import VIEWER, make_client

_ZONE = {
    "zone_id": "z1",
    "name": "Loading Bay",
    "kind": "loading",
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
    "rules": {"speed_limit_kph": 20},
}


def _create(client: TestClient) -> None:
    assert client.post("/sites/kn-zw-01/zones", json=_ZONE).status_code == 201


def test_list_and_get_zone(client: TestClient) -> None:
    _create(client)
    listing = client.get("/sites/kn-zw-01/zones").json()
    assert any(z["zone_id"] == "z1" for z in listing)
    one = client.get("/sites/kn-zw-01/zones/z1")
    assert one.status_code == 200
    assert one.json()["kind"] == "loading"


def test_get_missing_zone_404(client: TestClient) -> None:
    assert client.get("/sites/kn-zw-01/zones/nope").status_code == 404


def test_put_updates_and_audits(client: TestClient) -> None:
    _create(client)
    updated = {**_ZONE, "name": "Renamed Bay"}
    r = client.put("/sites/kn-zw-01/zones/z1", json=updated)
    assert r.status_code == 200
    assert client.get("/sites/kn-zw-01/zones/z1").json()["name"] == "Renamed Bay"
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "zone.upsert" and a["entity_id"] == "z1" for a in audit)


def test_put_path_body_mismatch_400(client: TestClient) -> None:
    _create(client)
    r = client.put("/sites/kn-zw-01/zones/z1", json={**_ZONE, "zone_id": "other"})
    assert r.status_code == 400


def test_delete_zone_and_audit(client: TestClient) -> None:
    _create(client)
    assert client.delete("/sites/kn-zw-01/zones/z1").status_code == 204
    assert client.get("/sites/kn-zw-01/zones/z1").status_code == 404
    assert client.delete("/sites/kn-zw-01/zones/z1").status_code == 404  # already gone
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "zone.delete" and a["entity_id"] == "z1" for a in audit)


def test_viewer_cannot_write_zones(client: TestClient, db_session: Session) -> None:
    _create(client)
    viewer = make_client(db_session, VIEWER)
    assert viewer.get("/sites/kn-zw-01/zones").status_code == 200  # read allowed
    assert viewer.put("/sites/kn-zw-01/zones/z1", json=_ZONE).status_code == 403
    assert viewer.delete("/sites/kn-zw-01/zones/z1").status_code == 403
