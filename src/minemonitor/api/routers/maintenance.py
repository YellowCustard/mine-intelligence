"""Maintenance domain API (Phase 5) — service plans, work orders, deterministic health.

Reads are viewer-level; work orders are supervisor-level and audited; service plans are
admin-level (configuration). Health is derived on read and returned as a
``maintenance.health.v1`` assessment — inferred, evidenced, advisory. The current
engine-hours input, when available, is read from the latest fuel transaction for the
asset (a measured cross-domain read); it is never fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_supervisor, require_viewer
from minemonitor.maintenance import service
from minemonitor.storage.db import get_db
from minemonitor.storage.models import FuelTransaction, MaintenancePlan, User, WorkOrder

router = APIRouter(tags=["maintenance"])


class PlanIn(BaseModel):
    asset_id: str = Field(min_length=1)
    component: str = "machine"
    interval_hours: float | None = Field(default=None, gt=0)
    interval_days: int | None = Field(default=None, gt=0)
    note: str | None = None


class WorkOrderIn(BaseModel):
    asset_id: str = Field(min_length=1)
    component: str = "machine"
    type: str = "service"
    at_engine_hours: float | None = Field(default=None, ge=0)
    note: str | None = None


class CompleteIn(BaseModel):
    at_engine_hours: float | None = Field(default=None, ge=0)


def _plan_dict(p: MaintenancePlan) -> dict[str, Any]:
    return {
        "id": p.id,
        "asset_id": p.asset_id,
        "component": p.component,
        "interval_hours": p.interval_hours,
        "interval_days": p.interval_days,
        "note": p.note,
    }


def _wo_dict(w: WorkOrder) -> dict[str, Any]:
    return {
        "id": w.id,
        "asset_id": w.asset_id,
        "component": w.component,
        "type": w.type,
        "status": w.status,
        "opened_at": w.opened_at,
        "closed_at": w.closed_at,
        "at_engine_hours": w.at_engine_hours,
        "note": w.note,
    }


def _latest_engine_hours(db: Session, site_id: str, asset_id: str) -> float | None:
    """The most recent measured engine-hours reading for an asset, from fuel records."""
    return db.execute(
        select(FuelTransaction.engine_hours)
        .where(
            FuelTransaction.site_id == site_id,
            FuelTransaction.asset_id == asset_id,
            FuelTransaction.engine_hours.is_not(None),
        )
        .order_by(FuelTransaction.ts.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.post("/sites/{site_id}/maintenance/plans", status_code=201)
def upsert_plan(
    site_id: str, body: PlanIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Create/update a service plan (admin — configuration)."""
    try:
        plan = service.upsert_plan(
            db,
            site_id=site_id,
            asset_id=body.asset_id,
            component=body.component,
            interval_hours=body.interval_hours,
            interval_days=body.interval_days,
            now=datetime.now(UTC),
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=admin.username,
        action="maintenance.plan.upsert",
        entity_type="maintenance_plan",
        entity_id=plan.id,
        site_id=site_id,
        detail={"asset_id": body.asset_id, "component": body.component},
    )
    db.commit()
    return _plan_dict(plan)


@router.get("/sites/{site_id}/maintenance/plans")
def list_plans(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_viewer)
) -> list[dict[str, Any]]:
    return [_plan_dict(p) for p in service.list_plans(db, site_id)]


@router.post("/sites/{site_id}/maintenance/work-orders", status_code=201)
def open_work_order(
    site_id: str,
    body: WorkOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Open a work order (supervisor; audited)."""
    now = datetime.now(UTC)
    try:
        wo = service.open_work_order(
            db,
            site_id=site_id,
            asset_id=body.asset_id,
            component=body.component,
            type=body.type,
            created_by=user.username,
            now=now,
            at_engine_hours=body.at_engine_hours,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="maintenance.work_order.open",
        entity_type="work_order",
        entity_id=wo.id,
        site_id=site_id,
        detail={"asset_id": body.asset_id, "type": body.type},
    )
    db.commit()
    return _wo_dict(wo)


@router.post("/sites/{site_id}/maintenance/work-orders/{wo_id}/complete")
def complete_work_order(
    site_id: str,
    wo_id: str,
    body: CompleteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Complete a work order (supervisor; audited). A completed service resets health."""
    wo = service.complete_work_order(
        db, site_id, wo_id, now=datetime.now(UTC), at_engine_hours=body.at_engine_hours
    )
    if wo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work order not found")
    audit.record(
        db,
        actor=user.username,
        action="maintenance.work_order.complete",
        entity_type="work_order",
        entity_id=wo.id,
        site_id=site_id,
        detail={"at_engine_hours": wo.at_engine_hours},
    )
    db.commit()
    return _wo_dict(wo)


@router.get("/sites/{site_id}/maintenance/work-orders")
def list_work_orders(
    site_id: str,
    asset_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [
        _wo_dict(w)
        for w in service.list_work_orders(
            db, site_id, asset_id=asset_id, status=status, limit=limit
        )
    ]


@router.get("/sites/{site_id}/maintenance/health")
def health(
    site_id: str,
    asset_id: str,
    component: str = "machine",
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """Deterministic maintenance health/risk for an asset component (inferred, evidenced)."""
    hours = _latest_engine_hours(db, site_id, asset_id)
    return service.assess_health(
        db, site_id, asset_id, component, now=datetime.now(UTC), current_engine_hours=hours
    )
