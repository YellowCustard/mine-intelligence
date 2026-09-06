"""HTTP coverage for downtime/delay classification: create, validate, list, auth."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.operations import delays
from minemonitor.storage.models import Position
from tests.conftest import SUPERVISOR, VIEWER, make_client

_WINDOW = {"start_ts": "2026-09-05T08:00:00Z", "end_ts": "2026-09-05T08:45:00Z"}


def test_categories_endpoint_lists_known_categories(client: TestClient) -> None:
    cats = client.get("/delay-categories").json()
    assert cats == list(delays.DELAY_CATEGORIES)
    assert "loader_unavailable" in cats and "unknown" in cats


def test_create_and_list_classification(client: TestClient) -> None:
    r = client.post(
        "/sites/kn-zw-01/delays",
        json={
            "category": "loader_unavailable",
            "asset_id": "HT-102",
            "note": "loader on B pit",
            **_WINDOW,
        },
    )
    assert r.status_code == 201
    row = r.json()
    assert row["category"] == "loader_unavailable" and row["source"] == "manual"

    listing = client.get("/sites/kn-zw-01/delays").json()
    assert any(d["id"] == row["id"] for d in listing)
    filtered = client.get("/sites/kn-zw-01/delays?category=loader_unavailable").json()
    assert all(d["category"] == "loader_unavailable" for d in filtered)


def test_unknown_category_rejected(client: TestClient) -> None:
    r = client.post("/sites/kn-zw-01/delays", json={"category": "not_a_category", **_WINDOW})
    assert r.status_code == 422


def test_non_positive_window_rejected(client: TestClient) -> None:
    r = client.post(
        "/sites/kn-zw-01/delays",
        json={
            "category": "maintenance",
            "start_ts": "2026-09-05T09:00:00Z",
            "end_ts": "2026-09-05T08:00:00Z",
        },
    )
    assert r.status_code == 422


def test_classification_never_touches_telemetry(client: TestClient, db_session: Session) -> None:
    before = db_session.execute(select(Position)).scalars().all()
    client.post("/sites/kn-zw-01/delays", json={"category": "breakdown", **_WINDOW})
    after = db_session.execute(select(Position)).scalars().all()
    assert len(before) == len(after) == 0  # positions untouched by an annotation


def test_delete_classification_is_audited(client: TestClient) -> None:
    rid = client.post("/sites/kn-zw-01/delays", json={"category": "weather", **_WINDOW}).json()[
        "id"
    ]
    assert client.delete(f"/sites/kn-zw-01/delays/{rid}").status_code == 204
    assert client.delete(f"/sites/kn-zw-01/delays/{rid}").status_code == 404
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "delay.delete" and a["entity_id"] == rid for a in audit)


def test_viewer_cannot_classify(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.get("/sites/kn-zw-01/delays").status_code == 200  # read ok
    r = viewer.post("/sites/kn-zw-01/delays", json={"category": "maintenance", **_WINDOW})
    assert r.status_code == 403


def test_supervisor_can_classify_and_is_audited(db_session: Session) -> None:
    sup = make_client(db_session, SUPERVISOR)
    r = sup.post("/sites/kn-zw-01/delays", json={"category": "refuelling", **_WINDOW})
    assert r.status_code == 201
    admin = make_client(db_session, ("admin", "testpass123"))
    audit = admin.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "delay.classify" for a in audit)
