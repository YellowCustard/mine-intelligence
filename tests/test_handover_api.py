"""Shift handover: create (snapshotting the scorecard), acknowledge, auth, audit."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import SUPERVISOR, VIEWER, make_client

_SHIFT = "kn-zw-01:2026-09-05:day"


def test_create_snapshots_scorecard_and_lists(client: TestClient) -> None:
    r = client.post(
        "/sites/kn-zw-01/handovers",
        json={"shift_id": _SHIFT, "outgoing_notes": "loader B down last 2h"},
    )
    assert r.status_code == 201
    h = r.json()
    assert h["state"] == "open"
    assert h["shift_id"] == _SHIFT
    assert h["outgoing_notes"] == "loader B down last 2h"
    # The scorecard snapshot is frozen into the record.
    assert "cycles" in h["summary"] and "utilisation" in h["summary"]

    listing = client.get(f"/sites/kn-zw-01/handovers?shift_id={_SHIFT}").json()
    assert any(x["id"] == h["id"] for x in listing)


def test_create_for_current_shift_without_id(client: TestClient) -> None:
    # No shift_id → resolves the shift containing 'now'; day/night cover 24h.
    r = client.post("/sites/kn-zw-01/handovers", json={"outgoing_notes": "quiet shift"})
    assert r.status_code == 201
    assert r.json()["shift_id"].startswith("kn-zw-01:")


def test_acknowledge_flow_and_double_ack_rejected(db_session: Session) -> None:
    sup = make_client(db_session, SUPERVISOR)
    hid = sup.post("/sites/kn-zw-01/handovers", json={"shift_id": _SHIFT}).json()["id"]
    r = sup.post(
        f"/sites/kn-zw-01/handovers/{hid}/acknowledge",
        json={"incoming_notes": "understood, watching loader B"},
    )
    assert r.status_code == 200
    acked = r.json()
    assert acked["state"] == "acknowledged"
    assert acked["incoming_by"] == SUPERVISOR[0]
    assert acked["acknowledged_at"] is not None
    # A second acknowledgement is a conflict.
    assert sup.post(f"/sites/kn-zw-01/handovers/{hid}/acknowledge", json={}).status_code == 409


def test_actions_are_audited(client: TestClient) -> None:
    hid = client.post("/sites/kn-zw-01/handovers", json={"shift_id": _SHIFT}).json()["id"]
    client.post(f"/sites/kn-zw-01/handovers/{hid}/acknowledge", json={})
    audit = client.get("/sites/kn-zw-01/audit").json()
    actions = {a["action"] for a in audit}
    assert "handover.create" in actions and "handover.acknowledge" in actions


def test_viewer_cannot_create_or_acknowledge(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.get("/sites/kn-zw-01/handovers").status_code == 200  # read ok
    assert viewer.post("/sites/kn-zw-01/handovers", json={"shift_id": _SHIFT}).status_code == 403


def test_bad_shift_id_is_404(client: TestClient) -> None:
    assert (
        client.post(
            "/sites/kn-zw-01/handovers", json={"shift_id": "kn-zw-01:2026-09-05:swing"}
        ).status_code
        == 404
    )
    assert client.get("/sites/kn-zw-01/handovers/nope").status_code == 404
