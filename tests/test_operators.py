"""Operators — the personal-data surface: FK identity, export, erasure, audit (M6+, brief §4)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import Event, HaulCycle, Operator
from tests.conftest import make_client

_NOW = datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)


def _seed_operator(client: TestClient) -> str:
    r = client.post(
        "/sites/kn-zw-01/operators",
        json={"display_name": "Alice Driver", "employee_ref": "E-1", "contact": "alice@x"},
    )
    assert r.status_code == 201, r.text
    return r.json()["operator_id"]


def _attach_references(db: Session, operator_id: str) -> None:
    """Point one event and one cycle at the operator by foreign key."""
    db.add(
        Event(
            event_id="evt-op-1",
            site_id="kn-zw-01",
            ts=_NOW,
            type="zone_breach",
            severity="critical",
            source="gnss_geofence",
            summary="x",
            advisory=True,
            state="open",
            operator_id=operator_id,
        )
    )
    db.add(
        HaulCycle(
            site_id="kn-zw-01",
            asset_id="HT-102",
            operator_id=operator_id,
            start_ts=_NOW,
            end_ts=_NOW,
            cycle_time_s=600.0,
            queue_s=60.0,
            load_s=120.0,
            haul_s=200.0,
            dump_s=100.0,
            return_s=120.0,
        )
    )
    db.commit()


def test_create_generates_opaque_id_not_a_name(client: TestClient) -> None:
    operator_id = _seed_operator(client)
    # Opaque reference — not the person's name.
    assert "Alice" not in operator_id
    assert operator_id.startswith("op-")


def test_identity_is_a_foreign_key_on_events_and_cycles(
    client: TestClient, db_session: Session
) -> None:
    operator_id = _seed_operator(client)
    _attach_references(db_session, operator_id)
    # The operational rows reference the operator by id, carrying no name.
    evt = db_session.get(Event, "evt-op-1")
    assert evt is not None and evt.operator_id == operator_id
    assert "Alice" not in (evt.summary or "")


def test_export_returns_record_and_references_and_is_audited(
    client: TestClient, db_session: Session
) -> None:
    operator_id = _seed_operator(client)
    _attach_references(db_session, operator_id)

    r = client.get(f"/sites/kn-zw-01/operators/{operator_id}/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operator"]["display_name"] == "Alice Driver"
    assert body["references"]["events"] == ["evt-op-1"]
    assert len(body["references"]["cycles"]) == 1

    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(
        a["action"] == "personal_data.export" and a["entity_id"] == operator_id for a in audit
    )


def test_read_is_audited_as_personal_data_access(client: TestClient) -> None:
    operator_id = _seed_operator(client)
    assert client.get(f"/sites/kn-zw-01/operators/{operator_id}").status_code == 200
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(
        a["action"] == "personal_data.access" and a["entity_id"] == operator_id for a in audit
    )


def test_erase_tombstones_pii_but_keeps_id_and_history(
    client: TestClient, db_session: Session
) -> None:
    operator_id = _seed_operator(client)
    _attach_references(db_session, operator_id)

    r = client.delete(f"/sites/kn-zw-01/operators/{operator_id}")
    assert r.status_code == 200, r.text
    assert r.json()["erased_at"] is not None

    # PII is gone; the opaque id and the row survive.
    db_session.expire_all()
    op = db_session.get(Operator, operator_id)
    assert op is not None
    assert op.display_name is None and op.employee_ref is None and op.contact is None
    assert op.erased_at is not None

    # Historical foreign keys still resolve — deletion did not rewrite history.
    evt = db_session.get(Event, "evt-op-1")
    assert evt is not None and evt.operator_id == operator_id
    cycles = (
        db_session.execute(select(HaulCycle).where(HaulCycle.operator_id == operator_id))
        .scalars()
        .all()
    )
    assert len(cycles) == 1

    # The erasure is auditable.
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "operator.erase" and a["entity_id"] == operator_id for a in audit)


def test_operator_is_site_scoped(db_session: Session) -> None:
    # A site-scoped admin for another site cannot reach kn-zw-01's operators.
    from minemonitor.auth.service import create_user

    create_user(
        db_session,
        username="other-admin",
        password="testpass123",
        role="admin",
        site_id="other-site",
    )
    db_session.commit()
    other = make_client(db_session, ("other-admin", "testpass123"))
    assert other.get("/sites/kn-zw-01/operators").status_code == 403
