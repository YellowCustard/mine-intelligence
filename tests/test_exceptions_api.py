"""The exception layer: what needs attention now, quiet when healthy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.operations import incidents
from tests.conftest import ADMIN, make_client


def _ingest(client: TestClient, asset_id: str, ts: datetime, *, ignition: bool) -> None:
    r = client.post(
        "/ingest/positions",
        json={
            "schema": "asset.position.v1",
            "site_id": "kn-zw-01",
            "asset_id": asset_id,
            "ts": ts.isoformat(),
            "lat": -17.8252,
            "lon": 31.0335,
            "speed_kph": 0.0,
            "ignition": ignition,
            "source": "test",
        },
    )
    assert r.status_code in (200, 201, 202), r.text


def test_healthy_when_nothing_to_act_on(client: TestClient) -> None:
    body = client.get("/sites/kn-zw-01/exceptions").json()
    assert body["healthy"] is True
    assert all(c == 0 for c in body["counts"].values())


def test_exceptions_surface_each_group(client: TestClient, db_session: Session) -> None:
    now = datetime.now(UTC)
    # A stopped machine: a *fresh* stationary, ignition-off fix.
    _ingest(client, "HT-102", now - timedelta(seconds=5), ignition=False)
    # An offline tracker: a stale fix, well past the offline threshold.
    _ingest(client, "LV-07", now - timedelta(hours=2), ignition=False)
    # A critical unresolved alarm.
    persist_event(
        db_session,
        EventV1(
            event_id=new_event_id(),
            site_id="kn-zw-01",
            ts=now,
            type="zone_breach",
            severity="critical",
            asset_id="LV-07",
            source="gnss_geofence",
            summary="unauthorised entry",
        ),
    )
    # An open, unassigned incident.
    incidents.create_incident(
        db_session,
        site_id="kn-zw-01",
        summary="loader down",
        type_="operational",
        severity="warning",
        actor="sup",
    )
    db_session.commit()

    body = client.get("/sites/kn-zw-01/exceptions").json()
    assert body["healthy"] is False
    assert body["counts"]["critical_alarms"] >= 1
    assert body["counts"]["unresolved_incidents"] >= 1
    assert body["counts"]["stopped_machines"] == 1
    assert body["counts"]["offline_trackers"] == 1

    # Stopped and offline are distinct groups — a comms outage is not downtime.
    stopped = {m["asset_id"] for m in body["groups"]["stopped_machines"]}
    offline = {m["asset_id"] for m in body["groups"]["offline_trackers"]}
    assert stopped == {"HT-102"} and offline == {"LV-07"}
    assert body["groups"]["unresolved_incidents"][0]["unassigned"] is True


def test_resolved_alarms_and_closed_incidents_drop_out(
    client: TestClient, db_session: Session
) -> None:
    incident = incidents.create_incident(
        db_session,
        site_id="kn-zw-01",
        summary="x",
        type_="operational",
        severity="info",
        actor="sup",
    )
    incidents.transition_incident(db_session, incident, actor="sup", to_state="closed", note="done")
    db_session.commit()
    body = client.get("/sites/kn-zw-01/exceptions").json()
    assert body["counts"]["unresolved_incidents"] == 0


def test_exceptions_require_viewer(db_session: Session) -> None:
    anon = make_client(db_session, None)
    assert anon.get("/sites/kn-zw-01/exceptions").status_code == 401
    admin = make_client(db_session, ADMIN)
    assert admin.get("/sites/kn-zw-01/exceptions").status_code == 200
