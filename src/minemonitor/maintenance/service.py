"""Maintenance domain service. Callers commit.

``assess_health`` is a **deterministic** indicator: it compares the time (days) and/or
engine-hours since the last completed service against the plan interval and bands the
result into a risk level. Every assessment carries its ``basis`` (observed | measured |
estimated | unknown), a ``confidence``, and the ``evidence`` — an inferred judgement,
never a measured fact, and never an instruction to stop a machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import MaintenancePlan, WorkOrder

_VALID_WO_TYPES = {"service", "repair", "inspection"}
_VALID_WO_STATUS = {"open", "in_progress", "done", "cancelled"}

_ACTION = {
    "Normal": "No action; within the service interval.",
    "Watch": "Plan the next service.",
    "Elevated": "Schedule service before the next major shift.",
    "High": "Service overdue — inspect and service promptly.",
    "Critical": "Service significantly overdue — inspect before further operation.",
    "Unknown": "Insufficient data — record a service baseline / engine-hour reading.",
}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _risk(fraction: float) -> str:
    if fraction < 0.75:
        return "Normal"
    if fraction < 0.90:
        return "Watch"
    if fraction < 1.00:
        return "Elevated"
    if fraction < 1.15:
        return "High"
    return "Critical"


# --- plans -------------------------------------------------------------------


def upsert_plan(
    session: Session,
    *,
    site_id: str,
    asset_id: str,
    component: str,
    interval_hours: float | None,
    interval_days: int | None,
    now: datetime,
    note: str | None = None,
) -> MaintenancePlan:
    if not interval_hours and not interval_days:
        raise ValueError("a plan needs interval_hours and/or interval_days")
    plan = get_plan(session, site_id, asset_id, component)
    if plan is None:
        plan = MaintenancePlan(id=str(ULID()), created_at=now)
        session.add(plan)
    plan.site_id = site_id
    plan.asset_id = asset_id
    plan.component = component
    plan.interval_hours = interval_hours
    plan.interval_days = interval_days
    plan.note = note
    return plan


def get_plan(
    session: Session, site_id: str, asset_id: str, component: str
) -> MaintenancePlan | None:
    return session.execute(
        select(MaintenancePlan).where(
            MaintenancePlan.site_id == site_id,
            MaintenancePlan.asset_id == asset_id,
            MaintenancePlan.component == component,
        )
    ).scalar_one_or_none()


def list_plans(session: Session, site_id: str) -> list[MaintenancePlan]:
    return list(
        session.execute(
            select(MaintenancePlan)
            .where(MaintenancePlan.site_id == site_id)
            .order_by(MaintenancePlan.asset_id, MaintenancePlan.component)
        ).scalars()
    )


# --- work orders -------------------------------------------------------------


def open_work_order(
    session: Session,
    *,
    site_id: str,
    asset_id: str,
    component: str,
    type: str,
    created_by: str,
    now: datetime,
    at_engine_hours: float | None = None,
    note: str | None = None,
) -> WorkOrder:
    if type not in _VALID_WO_TYPES:
        raise ValueError(f"type must be one of {sorted(_VALID_WO_TYPES)}")
    wo = WorkOrder(
        id=str(ULID()),
        site_id=site_id,
        asset_id=asset_id,
        component=component,
        type=type,
        status="open",
        opened_at=now,
        at_engine_hours=at_engine_hours,
        note=note,
        created_by=created_by,
        created_at=now,
    )
    session.add(wo)
    return wo


def complete_work_order(
    session: Session,
    site_id: str,
    wo_id: str,
    *,
    now: datetime,
    at_engine_hours: float | None = None,
) -> WorkOrder | None:
    wo = session.get(WorkOrder, wo_id)
    if wo is None or wo.site_id != site_id:
        return None
    wo.status = "done"
    wo.closed_at = now
    if at_engine_hours is not None:
        wo.at_engine_hours = at_engine_hours
    return wo


def list_work_orders(
    session: Session,
    site_id: str,
    *,
    asset_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[WorkOrder]:
    stmt = select(WorkOrder).where(WorkOrder.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(WorkOrder.asset_id == asset_id)
    if status is not None:
        stmt = stmt.where(WorkOrder.status == status)
    stmt = stmt.order_by(WorkOrder.opened_at.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())


def _latest_completed_service(
    session: Session, site_id: str, asset_id: str, component: str
) -> WorkOrder | None:
    return session.execute(
        select(WorkOrder)
        .where(
            WorkOrder.site_id == site_id,
            WorkOrder.asset_id == asset_id,
            WorkOrder.component == component,
            WorkOrder.type == "service",
            WorkOrder.status == "done",
        )
        .order_by(WorkOrder.closed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# --- deterministic health indicator ------------------------------------------


def assess_health(
    session: Session,
    site_id: str,
    asset_id: str,
    component: str = "machine",
    *,
    now: datetime,
    current_engine_hours: float | None = None,
) -> dict[str, Any]:
    """Derive a deterministic maintenance risk for one asset component.

    Considers a days dimension (observed) and/or an engine-hours dimension (measured,
    only when both a current reading and a service-baseline reading exist). The most
    urgent dimension sets the risk band. With no plan, or no baseline to measure from,
    the result is ``Unknown`` — the system never fabricates a schedule or a reading.
    """

    def _out(**kw: Any) -> dict[str, Any]:
        base = {
            "schema": "maintenance.health.v1",
            "site_id": site_id,
            "asset_id": asset_id,
            "component": component,
            "ts": now,
            "dimension": None,
            "fraction": None,
            "remaining": None,
            "evidence": [],
            "inferred": True,
        }
        base.update(kw)
        base["recommended_action"] = _ACTION[str(base["risk"])]
        return base

    plan = get_plan(session, site_id, asset_id, component)
    if plan is None:
        return _out(
            risk="Unknown",
            basis="unknown",
            confidence=0.0,
            evidence=["no maintenance plan defined for this asset/component"],
        )

    last = _latest_completed_service(session, site_id, asset_id, component)
    candidates: list[tuple[str, float, float, str, float, str]] = []

    if plan.interval_days:
        baseline = _aware(last.closed_at) if (last and last.closed_at) else _aware(plan.created_at)
        days_since = (now - baseline).total_seconds() / 86400
        label = "last service" if last else "plan start"
        candidates.append(
            (
                "days",
                days_since / plan.interval_days,
                round(plan.interval_days - days_since, 1),
                "observed",
                0.9,
                f"{days_since:.0f}d since {label}; interval {plan.interval_days}d",
            )
        )

    if (
        plan.interval_hours
        and current_engine_hours is not None
        and last is not None
        and last.at_engine_hours is not None
    ):
        hours_since = current_engine_hours - last.at_engine_hours
        if hours_since >= 0:
            candidates.append(
                (
                    "hours",
                    hours_since / plan.interval_hours,
                    round(plan.interval_hours - hours_since, 1),
                    "measured",
                    0.85,
                    f"{hours_since:.0f}h since last service "
                    f"({last.at_engine_hours:.0f}→{current_engine_hours:.0f}); "
                    f"interval {plan.interval_hours:.0f}h",
                )
            )

    if not candidates:
        ev = ["service plan defined but no baseline to measure from"]
        if plan.interval_hours and current_engine_hours is None:
            ev.append("engine-hours reading unavailable")
        if plan.interval_hours and (last is None or last.at_engine_hours is None):
            ev.append("no service-baseline engine hours recorded")
        return _out(risk="Unknown", basis="unknown", confidence=0.0, evidence=ev)

    dim, frac, remaining, basis, conf, _ev = max(candidates, key=lambda c: c[1])
    return _out(
        risk=_risk(frac),
        basis=basis,
        confidence=conf,
        dimension=dim,
        fraction=round(frac, 3),
        remaining=remaining,
        evidence=[c[5] for c in candidates],
    )
