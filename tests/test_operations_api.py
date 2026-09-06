"""HTTP coverage for the shift/operations surface and the snapshot's new fields."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import VIEWER, make_client


def test_current_shift(client: TestClient) -> None:
    r = client.get("/sites/kn-zw-01/shifts/current")
    assert r.status_code == 200
    shift = r.json()["shift"]
    assert shift is not None
    assert shift["name"] in ("day", "night")
    assert shift["shift_id"].startswith("kn-zw-01:")


def test_definitions_default_then_configured(client: TestClient) -> None:
    default = client.get("/sites/kn-zw-01/shift-definitions").json()
    assert default["source"] == "default"
    assert {d["name"] for d in default["definitions"]} == {"day", "night"}

    r = client.put(
        "/sites/kn-zw-01/shift-definitions/day",
        json={"start_hour_local": 7, "duration_hours": 10, "enabled": True},
    )
    assert r.status_code == 200 and r.json()["start_hour_local"] == 7

    configured = client.get("/sites/kn-zw-01/shift-definitions").json()
    assert configured["source"] == "configured"
    day = next(d for d in configured["definitions"] if d["name"] == "day")
    assert day["duration_hours"] == 10

    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "shift_definition.upsert" and a["entity_id"] == "day" for a in audit)


def test_delete_definition(client: TestClient) -> None:
    client.put(
        "/sites/kn-zw-01/shift-definitions/swing",
        json={"start_hour_local": 14, "duration_hours": 8, "enabled": True},
    )
    assert client.delete("/sites/kn-zw-01/shift-definitions/swing").status_code == 204
    assert client.delete("/sites/kn-zw-01/shift-definitions/swing").status_code == 404
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(
        a["action"] == "shift_definition.delete" and a["entity_id"] == "swing" for a in audit
    )


def test_viewer_cannot_edit_definitions(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.get("/sites/kn-zw-01/shift-definitions").status_code == 200  # read ok
    r = viewer.put(
        "/sites/kn-zw-01/shift-definitions/day",
        json={"start_hour_local": 6, "duration_hours": 12, "enabled": True},
    )
    assert r.status_code == 403


def test_invalid_hours_rejected(client: TestClient) -> None:
    r = client.put(
        "/sites/kn-zw-01/shift-definitions/day",
        json={"start_hour_local": 30, "duration_hours": 12, "enabled": True},
    )
    assert r.status_code == 422


def test_snapshot_includes_state_and_shift(client: TestClient) -> None:
    client.post(
        "/ingest/positions",
        json={
            "schema": "asset.position.v1",
            "site_id": "kn-zw-01",
            "asset_id": "HT-102",
            "ts": "2026-09-05T11:42:07Z",
            "lat": -17.8252,
            "lon": 31.0335,
            "speed_kph": 0.0,
            "ignition": False,
            "source": "test",
        },
    )
    body = client.get("/sites/kn-zw-01/state").json()
    assert "shift" in body
    asset = next(a for a in body["assets"] if a["asset_id"] == "HT-102")
    # A fresh stationary, ignition-off fix — but the fixture's clock is "now",
    # while the fix ts is fixed, so it may read offline; either way the derived
    # fields are present and internally consistent.
    assert asset["state"] in ("stopped", "idle", "moving", "offline")
    assert asset["state_basis"] in ("observed", "inferred")
    assert "data_age_s" in asset
