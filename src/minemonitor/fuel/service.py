"""Fuel domain service. Callers commit.

Recording is measured-only. ``consumption_summary`` and ``reconcile_tank`` compute
derived figures on read and label every value's ``basis`` (measured | calculated) so a
calculated efficiency is never mistaken for a metered fact. Reconciliation flags a
variance beyond tolerance; it never adjusts the stored records.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import FuelTank, FuelTankReading, FuelTransaction

_VALID_DIRECTIONS = {"dispense", "delivery"}


def create_tank(
    session: Session,
    *,
    tank_id: str,
    site_id: str,
    name: str,
    capacity_l: float | None,
    now: datetime,
) -> FuelTank:
    tank = session.get(FuelTank, tank_id)
    if tank is None:
        tank = FuelTank(tank_id=tank_id, created_at=now)
        session.add(tank)
    tank.site_id = site_id
    tank.name = name
    tank.capacity_l = capacity_l
    return tank


def list_tanks(session: Session, site_id: str) -> list[FuelTank]:
    rows = session.execute(
        select(FuelTank).where(FuelTank.site_id == site_id).order_by(FuelTank.tank_id)
    )
    return list(rows.scalars().all())


def record_transaction(
    session: Session,
    *,
    site_id: str,
    ts: datetime,
    litres: float,
    created_by: str,
    now: datetime,
    direction: str = "dispense",
    source: str = "manual",
    asset_id: str | None = None,
    tank_id: str | None = None,
    station: str | None = None,
    odometer_km: float | None = None,
    engine_hours: float | None = None,
    unit_cost: float | None = None,
    note: str | None = None,
) -> FuelTransaction:
    """Record one measured fuel transaction. Rejects non-positive litres / bad direction."""
    if litres <= 0:
        raise ValueError("litres must be positive")
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}")
    row = FuelTransaction(
        id=str(ULID()),
        site_id=site_id,
        asset_id=asset_id,
        tank_id=tank_id,
        ts=ts,
        litres=litres,
        direction=direction,
        source=source,
        station=station,
        odometer_km=odometer_km,
        engine_hours=engine_hours,
        unit_cost=unit_cost,
        note=note,
        created_at=now,
        created_by=created_by,
    )
    session.add(row)
    return row


def record_tank_reading(
    session: Session,
    *,
    site_id: str,
    tank_id: str,
    ts: datetime,
    level_l: float,
    now: datetime,
    source: str = "manual",
) -> FuelTankReading:
    row = FuelTankReading(
        id=str(ULID()),
        site_id=site_id,
        tank_id=tank_id,
        ts=ts,
        level_l=level_l,
        source=source,
        created_at=now,
    )
    session.add(row)
    return row


def list_transactions(
    session: Session,
    site_id: str,
    *,
    asset_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[FuelTransaction]:
    stmt = select(FuelTransaction).where(FuelTransaction.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(FuelTransaction.asset_id == asset_id)
    if since is not None:
        stmt = stmt.where(FuelTransaction.ts >= since)
    if until is not None:
        stmt = stmt.where(FuelTransaction.ts < until)
    stmt = stmt.order_by(FuelTransaction.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())


def consumption_summary(
    session: Session,
    site_id: str,
    *,
    since: datetime,
    until: datetime,
    asset_id: str,
) -> dict[str, Any]:
    """Per-asset consumption over a window.

    ``litres`` is measured (sum of dispenses). Distance and engine-hours are measured
    only when odometer/engine-hour readings bracket the window; the efficiency ratios
    are calculated from those measured inputs and are ``None`` when the input is absent
    — never fabricated. Each figure carries its ``basis``.
    """
    rows = [
        t
        for t in list_transactions(
            session, site_id, asset_id=asset_id, since=since, until=until, limit=1000
        )
        if t.direction == "dispense"
    ]
    litres = round(sum(t.litres for t in rows), 3)

    def _span(attr: str) -> float | None:
        vals = [getattr(t, attr) for t in rows if getattr(t, attr) is not None]
        return round(max(vals) - min(vals), 3) if len(vals) >= 2 else None

    distance_km = _span("odometer_km")
    engine_hours = _span("engine_hours")
    return {
        "asset_id": asset_id,
        "window": {"since": since, "until": until},
        "transactions": len(rows),
        "litres": {"value": litres, "basis": "measured"},
        "distance_km": {"value": distance_km, "basis": "measured"},
        "engine_hours": {"value": engine_hours, "basis": "measured"},
        "litres_per_km": {
            "value": round(litres / distance_km, 3) if distance_km else None,
            "basis": "calculated",
        },
        "litres_per_engine_hour": {
            "value": round(litres / engine_hours, 3) if engine_hours else None,
            "basis": "calculated",
        },
    }


def reconcile_tank(
    session: Session,
    site_id: str,
    tank_id: str,
    *,
    since: datetime,
    until: datetime,
    tolerance_l: float = 50.0,
) -> dict[str, Any]:
    """Reconcile a tank over a window: measured level change vs metered throughput.

    expected_closing = opening + deliveries_in − dispenses_out. A variance beyond
    ``tolerance_l`` is **flagged** with its evidence; the records are never changed.
    Needs at least two tank readings, else returns ``status: insufficient_data``.
    """
    readings = list(
        session.execute(
            select(FuelTankReading)
            .where(
                FuelTankReading.tank_id == tank_id,
                FuelTankReading.ts >= since,
                FuelTankReading.ts < until,
            )
            .order_by(FuelTankReading.ts)
        ).scalars()
    )
    if len(readings) < 2:
        return {"tank_id": tank_id, "status": "insufficient_data", "readings": len(readings)}

    opening, closing = readings[0].level_l, readings[-1].level_l
    txns = session.execute(
        select(FuelTransaction).where(
            FuelTransaction.tank_id == tank_id,
            FuelTransaction.ts >= readings[0].ts,
            FuelTransaction.ts <= readings[-1].ts,
        )
    ).scalars()
    deliveries = dispenses = 0.0
    for t in txns:
        if t.direction == "delivery":
            deliveries += t.litres
        else:
            dispenses += t.litres
    expected_closing = opening + deliveries - dispenses
    variance = round(closing - expected_closing, 3)
    return {
        "tank_id": tank_id,
        "status": "flagged" if abs(variance) > tolerance_l else "ok",
        "variance_l": variance,
        "tolerance_l": tolerance_l,
        "evidence": {
            "opening_l": round(opening, 3),
            "closing_l": round(closing, 3),
            "deliveries_l": round(deliveries, 3),
            "dispenses_l": round(dispenses, 3),
            "expected_closing_l": round(expected_closing, 3),
            "readings": len(readings),
            "basis": "measured",
        },
    }
