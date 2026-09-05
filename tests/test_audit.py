"""Audit trail on rule/zone changes and acknowledgements (M6, brief §4)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.storage.models import Event

_ZONE = {
    "zone_id": "r1", "name": "Magazine", "kind": "restricted",
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
    "rules": {"authorized_classes": []},
}


def test_zone_change_is_audited(client: TestClient) -> None:
    assert client.post("/sites/kn-zw-01/zones", json=_ZONE).status_code == 201
    audit = client.get("/sites/kn-zw-01/audit").json()
    actions = {a["action"]: a for a in audit}
    assert "zone.create" in actions
    assert actions["zone.create"]["entity_id"] == "r1"
    assert actions["zone.create"]["actor"] == "admin"


def test_ack_is_audited(client: TestClient, db_session: Session) -> None:
    db_session.add(
        Event(
            event_id="evt-1", site_id="kn-zw-01", ts=datetime.now(UTC), type="zone_breach",
            severity="critical", source="gnss_geofence", summary="LV-07 in magazine",
            advisory=True, state="open",
        )
    )
    db_session.commit()

    r = client.post("/sites/kn-zw-01/events/evt-1/ack", json={"note": "seen"})
    assert r.status_code == 200
    assert r.json()["state"] == "acknowledged"
    assert r.json()["acknowledged_by"] == "admin"

    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "event.ack" and a["entity_id"] == "evt-1" for a in audit)
