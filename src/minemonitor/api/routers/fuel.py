"""Fuel domain API (Phase 3) — measured transactions, tanks, consumption, reconciliation.

Reads are viewer-level; recording is supervisor-level and audited; tank reference data
is admin-level. Every write is site-scoped. Recording a transaction also publishes a
``fuel.transaction.v1`` on the in-process event bus (the Phase 1 seam) so future
domains (dispatch, analytics) can react without coupling to this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_supervisor, require_viewer
from minemonitor.contracts.fuel import FuelDirection, FuelSource, FuelTransactionV1
from minemonitor.fuel import service
from minemonitor.platform.bus import bus
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User

router = APIRouter(tags=["fuel"])


class TankIn(BaseModel):
    tank_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    capacity_l: float | None = Field(default=None, gt=0)


class TransactionIn(BaseModel):
    litres: float = Field(gt=0)
    ts: datetime | None = None
    direction: FuelDirection = "dispense"
    source: FuelSource = "manual"
    asset_id: str | None = None
    tank_id: str | None = None
    station: str | None = None
    odometer_km: float | None = Field(default=None, ge=0)
    engine_hours: float | None = Field(default=None, ge=0)
    unit_cost: float | None = Field(default=None, ge=0)
    note: str | None = None


class TankReadingIn(BaseModel):
    tank_id: str = Field(min_length=1)
    level_l: float = Field(ge=0)
    ts: datetime | None = None
    source: str = "manual"


def _txn_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "site_id": t.site_id,
        "asset_id": t.asset_id,
        "tank_id": t.tank_id,
        "ts": t.ts,
        "litres": t.litres,
        "direction": t.direction,
        "source": t.source,
        "station": t.station,
        "odometer_km": t.odometer_km,
        "engine_hours": t.engine_hours,
        "note": t.note,
    }


@router.post("/sites/{site_id}/fuel/tanks", status_code=201)
def create_tank(
    site_id: str,
    body: TankIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Create/update a fuel tank (admin — reference data)."""
    now = datetime.now(UTC)
    tank = service.create_tank(
        db,
        tank_id=body.tank_id,
        site_id=site_id,
        name=body.name,
        capacity_l=body.capacity_l,
        now=now,
    )
    audit.record(
        db,
        actor=admin.username,
        action="fuel.tank.upsert",
        entity_type="fuel_tank",
        entity_id=body.tank_id,
        site_id=site_id,
        detail={"name": body.name},
    )
    db.commit()
    return {
        "tank_id": tank.tank_id,
        "site_id": tank.site_id,
        "name": tank.name,
        "capacity_l": tank.capacity_l,
    }


@router.get("/sites/{site_id}/fuel/tanks")
def list_tanks(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_viewer)
) -> list[dict[str, Any]]:
    return [
        {"tank_id": t.tank_id, "site_id": t.site_id, "name": t.name, "capacity_l": t.capacity_l}
        for t in service.list_tanks(db, site_id)
    ]


@router.post("/sites/{site_id}/fuel/transactions", status_code=201)
def record_transaction(
    site_id: str,
    body: TransactionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Record a measured fuel transaction (supervisor; audited)."""
    now = datetime.now(UTC)
    try:
        row = service.record_transaction(
            db,
            site_id=site_id,
            ts=body.ts or now,
            litres=body.litres,
            created_by=user.username,
            now=now,
            direction=body.direction,
            source=body.source,
            asset_id=body.asset_id,
            tank_id=body.tank_id,
            station=body.station,
            odometer_km=body.odometer_km,
            engine_hours=body.engine_hours,
            unit_cost=body.unit_cost,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="fuel.transaction.record",
        entity_type="fuel_transaction",
        entity_id=row.id,
        site_id=site_id,
        detail={"litres": row.litres, "direction": row.direction},
    )
    db.commit()
    # Publish the versioned event on the in-process bus (Phase 1 seam). Best-effort:
    # a subscriber failure is isolated and never affects the recorded transaction.
    # Built via model_validate: request-model literals guarantee direction/source are
    # valid members, and this keeps the ORM str columns type-clean at the boundary.
    bus().publish(
        FuelTransactionV1.model_validate(
            {
                "transaction_id": row.id,
                "site_id": row.site_id,
                "ts": row.ts,
                "litres": row.litres,
                "direction": row.direction,
                "source": row.source,
                "asset_id": row.asset_id,
                "tank_id": row.tank_id,
                "station": row.station,
                "odometer_km": row.odometer_km,
                "engine_hours": row.engine_hours,
            }
        )
    )
    return _txn_dict(row)


@router.get("/sites/{site_id}/fuel/transactions")
def list_transactions(
    site_id: str,
    asset_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [
        _txn_dict(t)
        for t in service.list_transactions(
            db, site_id, asset_id=asset_id, since=since, until=until, limit=limit
        )
    ]


@router.post("/sites/{site_id}/fuel/tank-readings", status_code=201)
def record_tank_reading(
    site_id: str,
    body: TankReadingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Record a measured tank level reading (supervisor)."""
    now = datetime.now(UTC)
    row = service.record_tank_reading(
        db,
        site_id=site_id,
        tank_id=body.tank_id,
        ts=body.ts or now,
        level_l=body.level_l,
        now=now,
        source=body.source,
    )
    db.commit()
    return {"id": row.id, "tank_id": row.tank_id, "ts": row.ts, "level_l": row.level_l}


@router.get("/sites/{site_id}/fuel/consumption")
def consumption(
    site_id: str,
    asset_id: str,
    since: datetime,
    until: datetime,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """Per-asset consumption over a window (measured litres; calculated efficiency labelled)."""
    return service.consumption_summary(db, site_id, since=since, until=until, asset_id=asset_id)


@router.get("/sites/{site_id}/fuel/reconciliation/{tank_id}")
def reconciliation(
    site_id: str,
    tank_id: str,
    since: datetime,
    until: datetime,
    tolerance_l: float = 50.0,
    db: Session = Depends(get_db),
    _: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Reconcile a tank's measured level change against metered throughput (flags variance)."""
    return service.reconcile_tank(
        db, site_id, tank_id, since=since, until=until, tolerance_l=tolerance_l
    )
