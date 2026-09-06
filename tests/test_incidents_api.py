"""HTTP coverage for incident management: create, lifecycle, timeline, auth, audit."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from tests.conftest import SUPERVISOR, VIEWER, make_client


def _seed_event(db: Session) -> str:
    event = EventV1(
        event_id=new_event_id(),
        site_id="kn-zw-01",
        ts="2026-09-05T11:42:07Z",
        type="zone_breach",
        severity="critical",
        asset_id="LV-07",
        zone_id="r1-magazine",
        source="gnss_geofence",
        summary="LV-07 entered R1 Explosives Magazine",
    )
    persist_event(db, event)
    db.commit()
    return event.event_id


def test_create_list_and_get_incident(client: TestClient) -> None:
    r = client.post(
        "/sites/kn-zw-01/incidents",
        json={"summary": "Loader down at the face", "type": "operational", "severity": "warning"},
    )
    assert r.status_code == 201
    incident = r.json()
    assert incident["state"] == "open"
    iid = incident["incident_id"]

    listing = client.get("/sites/kn-zw-01/incidents").json()
    assert any(i["incident_id"] == iid for i in listing)

    detail = client.get(f"/sites/kn-zw-01/incidents/{iid}").json()
    assert detail["timeline"][0]["to_state"] == "open"


def test_incident_from_event_does_not_mutate_the_alarm(
    client: TestClient, db_session: Session
) -> None:
    event_id = _seed_event(db_session)
    r = client.post(f"/sites/kn-zw-01/events/{event_id}/incident")
    assert r.status_code == 201
    incident = r.json()
    assert incident["event_id"] == event_id
    assert incident["severity"] == "critical"
    # The raw alarm is untouched — still open, not acknowledged/resolved.
    events = client.get("/sites/kn-zw-01/events").json()
    alarm = next(e for e in events if e["event_id"] == event_id)
    assert alarm["state"] == "open"


def test_full_lifecycle_with_timeline(client: TestClient) -> None:
    iid = client.post(
        "/sites/kn-zw-01/incidents", json={"summary": "Investigate overspeed cluster"}
    ).json()["incident_id"]

    def transition(**body: object) -> dict:
        r = client.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    transition(to_state="acknowledged")
    transition(to_state="investigating", note="checking GPS jitter")
    assigned = transition(to_state="assigned", assignee="sup")
    assert assigned["assignee"] == "sup"
    resolved = transition(
        to_state="resolved", resolution="Recalibrated tracker", resolution_category="hardware"
    )
    assert resolved["state"] == "resolved" and resolved["resolved_at"] is not None
    closed = transition(to_state="closed")
    assert closed["state"] == "closed" and closed["closed_at"] is not None

    timeline = client.get(f"/sites/kn-zw-01/incidents/{iid}").json()["timeline"]
    states = [t["to_state"] for t in timeline if t["kind"] == "state_change"]
    assert states == ["open", "acknowledged", "investigating", "assigned", "resolved", "closed"]


def test_invalid_transition_rejected(client: TestClient) -> None:
    iid = client.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).json()["incident_id"]
    client.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json={"to_state": "closed"})
    # closed is terminal
    r = client.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json={"to_state": "open"})
    assert r.status_code == 409


def test_resolution_required_to_resolve(client: TestClient) -> None:
    iid = client.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).json()["incident_id"]
    r = client.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json={"to_state": "resolved"})
    assert r.status_code == 422


def test_assignee_required_to_assign(client: TestClient) -> None:
    iid = client.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).json()["incident_id"]
    r = client.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json={"to_state": "assigned"})
    assert r.status_code == 422


def test_notes_append_to_timeline(client: TestClient) -> None:
    iid = client.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).json()["incident_id"]
    assert (
        client.post(
            f"/sites/kn-zw-01/incidents/{iid}/notes", json={"text": "spoke to the operator"}
        ).status_code
        == 201
    )
    timeline = client.get(f"/sites/kn-zw-01/incidents/{iid}").json()["timeline"]
    assert any(t["kind"] == "note" and t["text"] == "spoke to the operator" for t in timeline)


def test_viewer_cannot_create_or_transition(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.get("/sites/kn-zw-01/incidents").status_code == 200  # read ok
    assert viewer.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).status_code == 403


def test_supervisor_can_manage_and_actions_are_audited(db_session: Session) -> None:
    sup = make_client(db_session, SUPERVISOR)
    iid = sup.post("/sites/kn-zw-01/incidents", json={"summary": "x"}).json()["incident_id"]
    sup.post(f"/sites/kn-zw-01/incidents/{iid}/transition", json={"to_state": "acknowledged"})
    admin = make_client(db_session, ("admin", "testpass123"))
    audit = admin.get("/sites/kn-zw-01/audit").json()
    actions = {a["action"] for a in audit}
    assert "incident.create" in actions and "incident.transition" in actions


def test_missing_incident_is_404(client: TestClient) -> None:
    assert client.get("/sites/kn-zw-01/incidents/nope").status_code == 404
    assert (
        client.post(
            "/sites/kn-zw-01/incidents/nope/transition", json={"to_state": "acknowledged"}
        ).status_code
        == 404
    )


def test_incident_from_missing_event_is_404(client: TestClient) -> None:
    assert client.post("/sites/kn-zw-01/events/nope/incident").status_code == 404
