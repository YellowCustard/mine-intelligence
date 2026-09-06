"""Accounts, operators (personal data), audit log and retention — the admin surface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor import audit, retention
from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.auth.service import ROLES, create_user
from minemonitor.storage.db import get_db
from minemonitor.storage.models import AuditLog, Event, HaulCycle, Operator, User

router = APIRouter(tags=["account"])


class UserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(viewer|supervisor|admin|device)$")
    site_id: str | None = None


class OperatorIn(BaseModel):
    """Create an operator. ``operator_id`` is optional — a server-generated
    opaque reference is used when omitted (never derive it from a name)."""

    operator_id: str | None = None
    display_name: str | None = None
    employee_ref: str | None = None
    contact: str | None = None


@router.get("/me")
def me(user: User = Depends(require_viewer)) -> dict[str, Any]:
    """The authenticated user's identity, role and site scope."""
    return {"username": user.username, "role": user.role, "site_id": user.site_id}


@router.post("/users", status_code=201)
def add_user(
    body: UserIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict[str, Any]:
    """Create or replace a user (admin only)."""
    if body.role not in ROLES:
        # Defensive; the pattern already constrains this.
        raise ValueError(f"bad role {body.role!r}")
    create_user(
        db, username=body.username, password=body.password, role=body.role, site_id=body.site_id
    )
    audit.record(
        db,
        actor=admin.username,
        action="user.create",
        entity_type="user",
        entity_id=body.username,
        site_id=body.site_id,
        detail={"role": body.role},
    )
    db.commit()
    return {"username": body.username, "role": body.role, "site_id": body.site_id}


# --- Operators: the single home for personal data (brief §4) ---------------
#
# Identity is a foreign key on events/cycles, never a name in a payload. These
# endpoints are the export/erase surface a data-subject request runs through, and
# every read of a personal record is itself audited. All are admin-only and
# path-scoped, so `require_admin` also confines a site-scoped admin to their site.


def _operator_dict(op: Operator) -> dict[str, Any]:
    return {
        "operator_id": op.operator_id,
        "site_id": op.site_id,
        "display_name": op.display_name,
        "employee_ref": op.employee_ref,
        "contact": op.contact,
        "created_at": op.created_at,
        "erased_at": op.erased_at,
    }


def _get_operator(db: Session, site_id: str, operator_id: str) -> Operator:
    op = db.get(Operator, operator_id)
    if op is None or op.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "operator not found")
    return op


@router.post("/sites/{site_id}/operators", status_code=201)
def create_operator(
    site_id: str,
    body: OperatorIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Register an operator (admin only). Personal data lives only here."""
    operator_id = body.operator_id or f"op-{ULID()}"
    if db.get(Operator, operator_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "operator_id already exists")
    op = Operator(
        operator_id=operator_id,
        site_id=site_id,
        display_name=body.display_name,
        employee_ref=body.employee_ref,
        contact=body.contact,
        created_at=datetime.now(UTC),
    )
    db.add(op)
    audit.record(
        db,
        actor=admin.username,
        action="operator.create",
        entity_type="operator",
        entity_id=operator_id,
        site_id=site_id,
    )
    db.commit()
    return _operator_dict(op)


@router.get("/sites/{site_id}/operators")
def list_operators(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)
) -> list[dict[str, Any]]:
    """List operators at a site (admin only). Listing is not per-record access."""
    rows = db.execute(select(Operator).where(Operator.site_id == site_id)).scalars().all()
    return [_operator_dict(op) for op in rows]


@router.get("/sites/{site_id}/operators/{operator_id}")
def read_operator(
    site_id: str,
    operator_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Read one operator's personal record. This access is audited (brief §4)."""
    op = _get_operator(db, site_id, operator_id)
    audit.record(
        db,
        actor=admin.username,
        action="personal_data.access",
        entity_type="operator",
        entity_id=operator_id,
        site_id=site_id,
    )
    db.commit()
    return _operator_dict(op)


@router.get("/sites/{site_id}/operators/{operator_id}/export")
def export_operator(
    site_id: str,
    operator_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Data-subject export: the operator record plus references to their data."""
    op = _get_operator(db, site_id, operator_id)
    event_ids = list(
        db.execute(
            select(Event.event_id).where(Event.site_id == site_id, Event.operator_id == operator_id)
        )
        .scalars()
        .all()
    )
    cycles = list(
        db.execute(
            select(HaulCycle.asset_id, HaulCycle.start_ts).where(
                HaulCycle.site_id == site_id, HaulCycle.operator_id == operator_id
            )
        ).all()
    )
    audit.record(
        db,
        actor=admin.username,
        action="personal_data.export",
        entity_type="operator",
        entity_id=operator_id,
        site_id=site_id,
        detail={"events": len(event_ids), "cycles": len(cycles)},
    )
    db.commit()
    return {
        "operator": _operator_dict(op),
        "references": {
            "events": event_ids,
            "cycles": [{"asset_id": a, "start_ts": s} for a, s in cycles],
        },
    }


@router.delete("/sites/{site_id}/operators/{operator_id}")
def erase_operator(
    site_id: str,
    operator_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Erasure request: tombstone the PII, keep the opaque id and history intact.

    The personal columns are nulled and ``erased_at`` stamped; the row and its
    ``operator_id`` remain so historical foreign keys (events, cycles) stay valid
    — deletion of the person never rewrites the operational record (brief §4).
    """
    op = _get_operator(db, site_id, operator_id)
    already = op.erased_at is not None
    op.display_name = None
    op.employee_ref = None
    op.contact = None
    if not already:
        op.erased_at = datetime.now(UTC)
    audit.record(
        db,
        actor=admin.username,
        action="operator.erase",
        entity_type="operator",
        entity_id=operator_id,
        site_id=site_id,
        detail={"already_erased": already},
    )
    db.commit()
    return {"operator_id": operator_id, "erased_at": op.erased_at}


@router.get("/sites/{site_id}/audit")
def get_audit(
    site_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Read the audit trail for a site (admin only)."""
    rows = list(
        db.execute(
            select(AuditLog)
            .where((AuditLog.site_id == site_id) | (AuditLog.site_id.is_(None)))
            .order_by(AuditLog.ts.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "ts": r.ts,
            "actor": r.actor,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "site_id": r.site_id,
            "detail": r.detail,
        }
        for r in rows
    ]


@router.post("/admin/retention/run")
def run_retention(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict[str, int]:
    """Run the retention deletion job now (global admin only). Also scheduled."""
    if admin.site_id is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "retention is a global-admin action")
    return retention.run_from_config(db)
