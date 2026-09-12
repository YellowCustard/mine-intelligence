"""Fuel domain: measured recording, calculated-vs-measured labelling, reconciliation, API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from minemonitor.fuel import service
from minemonitor.platform import contracts
from minemonitor.platform.bus import bus
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

_T0 = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


def _now() -> datetime:
    return datetime(2026, 9, 5, 18, 0, tzinfo=UTC)


# --- service -----------------------------------------------------------------


def test_record_rejects_bad_input(db_session: Session) -> None:
    with pytest.raises(ValueError, match="positive"):
        service.record_transaction(
            db_session, site_id="kn-zw-01", ts=_T0, litres=0, created_by="sup", now=_now()
        )
    with pytest.raises(ValueError, match="direction"):
        service.record_transaction(
            db_session,
            site_id="kn-zw-01",
            ts=_T0,
            litres=5,
            created_by="sup",
            now=_now(),
            direction="sideways",
        )


def test_consumption_labels_measured_vs_calculated(db_session: Session) -> None:
    # Two dispenses with odometer + engine-hour readings bracketing the window.
    service.record_transaction(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        ts=_T0,
        litres=100,
        created_by="s",
        now=_now(),
        odometer_km=1000.0,
        engine_hours=500.0,
    )
    service.record_transaction(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        ts=_T0 + timedelta(hours=6),
        litres=140,
        created_by="s",
        now=_now(),
        odometer_km=1200.0,
        engine_hours=510.0,
    )
    db_session.commit()
    s = service.consumption_summary(
        db_session, "kn-zw-01", since=_T0 - timedelta(hours=1), until=_now(), asset_id="HT-102"
    )
    assert s["litres"] == {"value": 240.0, "basis": "measured"}
    assert s["distance_km"]["value"] == 200.0 and s["distance_km"]["basis"] == "measured"
    # 240 L over 200 km → 1.2 L/km, explicitly calculated.
    assert s["litres_per_km"] == {"value": 1.2, "basis": "calculated"}
    assert s["litres_per_engine_hour"]["value"] == 24.0


def test_consumption_omits_ratios_without_measured_denominator(db_session: Session) -> None:
    service.record_transaction(
        db_session,
        site_id="kn-zw-01",
        asset_id="EX-01",
        ts=_T0,
        litres=80,
        created_by="s",
        now=_now(),
    )
    db_session.commit()
    s = service.consumption_summary(
        db_session, "kn-zw-01", since=_T0 - timedelta(hours=1), until=_now(), asset_id="EX-01"
    )
    assert s["litres"]["value"] == 80.0
    # No odometer/engine-hours → ratios are None, never fabricated.
    assert s["litres_per_km"]["value"] is None
    assert s["litres_per_engine_hour"]["value"] is None


def test_reconcile_flags_variance_beyond_tolerance(db_session: Session) -> None:
    service.create_tank(
        db_session, tank_id="TANK-1", site_id="kn-zw-01", name="Main", capacity_l=20000, now=_now()
    )
    # Opening 10000, deliver 5000, dispense 3000 → expected closing 12000. But the tank
    # actually reads 11500 at close → 500 L unaccounted for.
    service.record_tank_reading(
        db_session, site_id="kn-zw-01", tank_id="TANK-1", ts=_T0, level_l=10000, now=_now()
    )
    service.record_transaction(
        db_session,
        site_id="kn-zw-01",
        tank_id="TANK-1",
        ts=_T0 + timedelta(hours=1),
        litres=5000,
        direction="delivery",
        created_by="s",
        now=_now(),
    )
    service.record_transaction(
        db_session,
        site_id="kn-zw-01",
        tank_id="TANK-1",
        ts=_T0 + timedelta(hours=2),
        litres=3000,
        direction="dispense",
        created_by="s",
        now=_now(),
    )
    service.record_tank_reading(
        db_session,
        site_id="kn-zw-01",
        tank_id="TANK-1",
        ts=_T0 + timedelta(hours=3),
        level_l=11500,
        now=_now(),
    )
    db_session.commit()
    window = {"since": _T0 - timedelta(hours=1), "until": _now()}

    flagged = service.reconcile_tank(db_session, "kn-zw-01", "TANK-1", **window)
    assert flagged["status"] == "flagged" and flagged["variance_l"] == -500.0
    assert flagged["evidence"]["expected_closing_l"] == 12000.0
    # The same 500 L variance is within a generous tolerance → not flagged (never corrected).
    lenient = service.reconcile_tank(db_session, "kn-zw-01", "TANK-1", tolerance_l=1000, **window)
    assert lenient["status"] == "ok" and lenient["variance_l"] == -500.0


def test_reconcile_insufficient_data(db_session: Session) -> None:
    service.create_tank(
        db_session, tank_id="TANK-2", site_id="kn-zw-01", name="Aux", capacity_l=None, now=_now()
    )
    db_session.commit()
    r = service.reconcile_tank(db_session, "kn-zw-01", "TANK-2", since=_T0, until=_now())
    assert r["status"] == "insufficient_data"


# --- contract / bus ----------------------------------------------------------


def test_fuel_contract_registered() -> None:
    assert "fuel.transaction.v1" in contracts.registered()


# --- API ---------------------------------------------------------------------


def test_supervisor_records_and_event_is_published(db_session: Session) -> None:
    captured: list = []
    bus().subscribe("fuel.transaction.v1", lambda e: captured.append(e))
    try:
        c = make_client(db_session, SUPERVISOR)
        r = c.post(
            "/sites/kn-zw-01/fuel/transactions",
            json={"litres": 120.5, "asset_id": "HT-102", "direction": "dispense"},
        )
        assert r.status_code == 201 and r.json()["litres"] == 120.5
        # The versioned event flowed on the bus with the measured litres.
        assert len(captured) == 1 and captured[0].litres == 120.5
        assert captured[0].schema_ == "fuel.transaction.v1"
    finally:
        bus().clear()


def test_viewer_cannot_record(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    assert c.post("/sites/kn-zw-01/fuel/transactions", json={"litres": 10}).status_code == 403


def test_tank_is_admin_only_and_recording_is_audited(db_session: Session) -> None:
    assert (
        make_client(db_session, SUPERVISOR)
        .post("/sites/kn-zw-01/fuel/tanks", json={"tank_id": "T", "name": "x"})
        .status_code
        == 403
    )
    admin = make_client(db_session, ADMIN)
    assert (
        admin.post("/sites/kn-zw-01/fuel/tanks", json={"tank_id": "T", "name": "Main"}).status_code
        == 201
    )
    make_client(db_session, SUPERVISOR).post(
        "/sites/kn-zw-01/fuel/transactions", json={"litres": 50}
    )
    audit = admin.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "fuel.transaction.record" for a in audit)


def test_v1_prefix_serves_fuel(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    assert c.get("/api/v1/sites/kn-zw-01/fuel/transactions").status_code == 200
