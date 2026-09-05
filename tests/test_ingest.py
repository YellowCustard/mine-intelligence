"""M1 acceptance: a position POSTed is validated, stored, and readable back."""

from __future__ import annotations

from fastapi.testclient import TestClient

_POSITION = {
    "schema": "asset.position.v1",
    "site_id": "kn-zw-01",
    "asset_id": "HT-102",
    "ts": "2026-09-05T11:42:07Z",
    "lat": -17.8252,
    "lon": 31.0335,
    "speed_kph": 47.0,
    "heading_deg": 118,
    "ignition": True,
    "source": "simulator",
}


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_post_then_read_back(client: TestClient) -> None:
    post = client.post("/ingest/positions", json=_POSITION)
    assert post.status_code == 202
    assert post.json() == {"stored": True, "created": True}

    read = client.get("/sites/kn-zw-01/positions", params={"asset_id": "HT-102"})
    assert read.status_code == 200
    rows = read.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["asset_id"] == "HT-102"
    assert row["lat"] == -17.8252
    assert row["source"] == "simulator"
    # received_at is stamped server-side, never sent by the device.
    assert row["received_at"] is not None


def test_replay_is_idempotent(client: TestClient) -> None:
    """Replaying the same position must not duplicate it (brief §12)."""
    first = client.post("/ingest/positions", json=_POSITION)
    assert first.json()["created"] is True
    second = client.post("/ingest/positions", json=_POSITION)
    assert second.status_code == 202
    assert second.json()["created"] is False

    rows = client.get("/sites/kn-zw-01/positions").json()
    assert len(rows) == 1


def test_malformed_position_rejected_loudly(client: TestClient) -> None:
    """Out-of-range latitude is rejected, not silently coerced (brief §12)."""
    bad = {**_POSITION, "lat": 999}
    resp = client.post("/ingest/positions", json=bad)
    assert resp.status_code == 422


def test_unknown_field_rejected(client: TestClient) -> None:
    """extra='forbid' means unexpected device fields are rejected."""
    bad = {**_POSITION, "payload_tonnes": 42}
    resp = client.post("/ingest/positions", json=bad)
    assert resp.status_code == 422


def test_read_is_site_scoped(client: TestClient) -> None:
    client.post("/ingest/positions", json=_POSITION)
    other = client.get("/sites/other-site/positions").json()
    assert other == []
