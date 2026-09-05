"""Smoke test for the dashboard state snapshot endpoint (M5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

_POSITION = {
    "schema": "asset.position.v1",
    "site_id": "kn-zw-01",
    "asset_id": "HT-102",
    "ts": "2026-09-05T11:42:07Z",
    "lat": -17.8252,
    "lon": 31.0335,
    "speed_kph": 12.0,
    "ignition": True,
    "source": "test",
}


def test_state_snapshot_shape(client: TestClient) -> None:
    client.post("/ingest/positions", json=_POSITION)
    resp = client.get("/sites/kn-zw-01/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["site_id"] == "kn-zw-01"
    assert {"assets", "events", "zones", "cycles"} <= set(body)
    # The ingested asset appears with its class from the registry.
    ids = {a["asset_id"] for a in body["assets"]}
    assert "HT-102" in ids
    asset = next(a for a in body["assets"] if a["asset_id"] == "HT-102")
    assert asset["asset_class"] == "haul_truck"
    # Cycle summary is present and well-formed even with no cycles yet.
    assert body["cycles"]["cycles"] == 0


def test_dashboard_is_served() -> None:
    from minemonitor.api.main import create_app

    with TestClient(create_app()) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert "Mine Monitor" in resp.text
