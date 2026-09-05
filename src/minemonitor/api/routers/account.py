"""Accounts, audit log and retention — the M6 admin surface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import audit, retention
from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.auth.service import ROLES, create_user
from minemonitor.storage.db import get_db
from minemonitor.storage.models import AuditLog, User

router = APIRouter(tags=["account"])


class UserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(viewer|supervisor|admin|device)$")
    site_id: str | None = None


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
