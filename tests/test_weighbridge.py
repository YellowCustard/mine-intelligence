"""Weighbridge domain: measured tickets, idempotency, net-consistency, tonnage, CSV, API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from minemonitor.platform import contracts
from minemonitor.platform.bus import bus
from minemonitor.weighbridge import service
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

_T0 = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


def _now() -> datetime:
    return datetime(2026, 9, 5, 18, 0, tzinfo=UTC)


# --- service -----------------------------------------------------------------


def test_record_is_idempotent_per_ticket_no(db_session: Session) -> None:
    row1, created1 = service.record_ticket(
        db_session,
        site_id="kn-zw-01",
        ticket_no="T-100",
        ts=_T0,
        gross_kg=40000,
        tare_kg=15000,
        net_kg=25000,
        created_by="s",
        now=_now(),
        material="gold_ore",
    )
    db_session.commit()
    assert created1 is True
    # Re-recording the same ticket_no returns the existing row, does not double-count.
    row2, created2 = service.record_ticket(
        db_session,
        site_id="kn-zw-01",
        ticket_no="T-100",
        ts=_T0,
        gross_kg=99999,
        tare_kg=0,
        net_kg=99999,
        created_by="s",
        now=_now(),
    )
    assert created2 is False and row2.id == row1.id and row2.net_kg == 25000


def test_record_rejects_bad_input(db_session: Session) -> None:
    with pytest.raises(ValueError, match="direction"):
        service.record_ticket(
            db_session,
            site_id="kn-zw-01",
            ticket_no="T-x",
            ts=_T0,
            gross_kg=1,
            tare_kg=0,
            net_kg=1,
            created_by="s",
            now=_now(),
            direction="up",
        )
    with pytest.raises(ValueError, match="non-negative"):
        service.record_ticket(
            db_session,
            site_id="kn-zw-01",
            ticket_no="T-y",
            ts=_T0,
            gross_kg=-1,
            tare_kg=0,
            net_kg=1,
            created_by="s",
            now=_now(),
        )


def test_net_consistency_flags_mismatch(db_session: Session) -> None:
    good, _ = service.record_ticket(
        db_session,
        site_id="kn-zw-01",
        ticket_no="T-1",
        ts=_T0,
        gross_kg=40000,
        tare_kg=15000,
        net_kg=25000,
        created_by="s",
        now=_now(),
    )
    bad, _ = service.record_ticket(
        db_session,
        site_id="kn-zw-01",
        ticket_no="T-2",
        ts=_T0,
        gross_kg=40000,
        tare_kg=15000,
        net_kg=26000,
        created_by="s",
        now=_now(),  # 1000 kg off gross-tare
    )
    assert service.net_consistency(good)["consistent"] is True
    flag = service.net_consistency(bad)
    assert flag["consistent"] is False and flag["delta_kg"] == 1000.0
    assert flag["expected_net_kg"] == 25000.0


def test_tonnage_summary_measured_by_material(db_session: Session) -> None:
    for i, (mat, net) in enumerate([("gold_ore", 25000), ("gold_ore", 24000), ("waste", 30000)]):
        service.record_ticket(
            db_session,
            site_id="kn-zw-01",
            ticket_no=f"T-{i}",
            ts=_T0 + timedelta(minutes=i),
            gross_kg=net + 15000,
            tare_kg=15000,
            net_kg=net,
            created_by="s",
            now=_now(),
            material=mat,
        )
    db_session.commit()
    s = service.tonnage_summary(
        db_session, "kn-zw-01", since=_T0 - timedelta(hours=1), until=_now()
    )
    assert s["tickets"] == 3
    assert s["materials"]["gold_ore"] == {
        "tickets": 2,
        "net_kg": 49000.0,
        "net_tonnes": 49.0,
        "basis": "measured",
    }
    assert s["materials"]["waste"]["net_tonnes"] == 30.0
    assert s["net_inconsistent_tickets"] == 0


def test_csv_import_is_idempotent_and_collects_errors(db_session: Session) -> None:
    csv_text = (
        "ticket_no,ts,gross_kg,tare_kg,net_kg,material\n"
        "T-1,2026-09-05T07:00:00Z,40000,15000,25000,gold_ore\n"
        "T-2,2026-09-05T07:10:00Z,41000,15000,26000,gold_ore\n"
        "T-3,not-a-date,1,2,3,waste\n"  # bad row → error, others still import
    )
    r1 = service.import_csv(
        db_session, site_id="kn-zw-01", csv_text=csv_text, created_by="s", now=_now()
    )
    db_session.commit()
    assert r1["imported"] == 2 and len(r1["errors"]) == 1 and r1["errors"][0]["row"] == 4
    # Re-importing the same file skips the already-present tickets (idempotent).
    r2 = service.import_csv(
        db_session, site_id="kn-zw-01", csv_text=csv_text, created_by="s", now=_now()
    )
    assert r2["imported"] == 0 and r2["skipped"] == 2


def test_csv_import_rejects_missing_columns(db_session: Session) -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        service.import_csv(
            db_session,
            site_id="kn-zw-01",
            csv_text="ticket_no,ts\nT-1,2026-09-05T07:00:00Z\n",
            created_by="s",
            now=_now(),
        )


# --- contract / API ----------------------------------------------------------


def test_weighbridge_contract_registered() -> None:
    assert "weighbridge.transaction.v1" in contracts.registered()


def test_supervisor_records_and_publishes_once(db_session: Session) -> None:
    captured: list = []
    bus().subscribe("weighbridge.transaction.v1", lambda e: captured.append(e))
    try:
        c = make_client(db_session, SUPERVISOR)
        payload = {
            "ticket_no": "T-500",
            "gross_kg": 40000,
            "tare_kg": 15000,
            "net_kg": 25000,
            "material": "gold_ore",
            "asset_id": "HT-102",
        }
        r = c.post("/sites/kn-zw-01/weighbridge/tickets", json=payload)
        assert r.status_code == 201 and r.json()["created"] is True
        # Idempotent re-post: not created, not re-published.
        r2 = c.post("/sites/kn-zw-01/weighbridge/tickets", json=payload)
        assert r2.json()["created"] is False
        assert len(captured) == 1 and captured[0].net_kg == 25000
    finally:
        bus().clear()


def test_viewer_cannot_record_but_can_read(db_session: Session) -> None:
    v = make_client(db_session, VIEWER)
    assert (
        v.post(
            "/sites/kn-zw-01/weighbridge/tickets",
            json={"ticket_no": "x", "gross_kg": 1, "tare_kg": 0, "net_kg": 1},
        ).status_code
        == 403
    )
    assert v.get("/sites/kn-zw-01/weighbridge/tickets").status_code == 200
    assert v.get("/api/v1/sites/kn-zw-01/weighbridge/tickets").status_code == 200


def test_scale_is_admin_only(db_session: Session) -> None:
    assert (
        make_client(db_session, SUPERVISOR)
        .post("/sites/kn-zw-01/weighbridge/scales", json={"scale_id": "WB1", "name": "Main"})
        .status_code
        == 403
    )
    assert (
        make_client(db_session, ADMIN)
        .post("/sites/kn-zw-01/weighbridge/scales", json={"scale_id": "WB1", "name": "Main"})
        .status_code
        == 201
    )
