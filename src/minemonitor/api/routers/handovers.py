"""Shift handover: the outgoing crew records the shift, the incoming crew acks it.

Reads are viewer-level; writing or acknowledging a handover requires supervisor and
is audited. The handover snapshots the shift scorecard at creation time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.operations import handover as handover_mod
from minemonitor.operations.shifts import resolve_shift, resolve_shift_by_id
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User

router = APIRouter(tags=["handovers"])


class HandoverIn(BaseModel):
    shift_id: str | None = None  # default: the shift containing now
    outgoing_notes: str | None = None


class AckHandoverIn(BaseModel):
    incoming_notes: str | None = None


@router.get("/sites/{site_id}/handovers", dependencies=[Depends(require_viewer)])
def list_handovers(
    site_id: str,
    shift_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [
        handover_mod.as_dict(h) for h in handover_mod.list_handovers(db, site_id, shift_id=shift_id)
    ]


@router.get("/sites/{site_id}/handovers/{handover_id}", dependencies=[Depends(require_viewer)])
def get_handover(site_id: str, handover_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = handover_mod.get_handover(db, site_id, handover_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handover not found")
    return handover_mod.as_dict(row)


@router.post("/sites/{site_id}/handovers", status_code=201)
def create_handover(
    site_id: str,
    body: HandoverIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Write a handover for a shift (default: the current shift). Snapshots the scorecard."""
    if body.shift_id is not None:
        window = resolve_shift_by_id(db, site_id, body.shift_id)
    else:
        window = resolve_shift(db, site_id, datetime.now(UTC))
    if window is None:
        raise HTTPException(status_code=404, detail="no shift matches")
    row = handover_mod.create_handover(
        db,
        site_id=site_id,
        window=window,
        outgoing_by=user.username,
        outgoing_notes=body.outgoing_notes,
    )
    audit.record(
        db,
        actor=user.username,
        action="handover.create",
        entity_type="handover",
        entity_id=row.id,
        site_id=site_id,
        detail={"shift_id": window.shift_id},
    )
    db.commit()
    return handover_mod.as_dict(row)


@router.post("/sites/{site_id}/handovers/{handover_id}/acknowledge")
def acknowledge_handover(
    site_id: str,
    handover_id: str,
    body: AckHandoverIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Incoming crew acknowledges a handover. Audited."""
    row = handover_mod.get_handover(db, site_id, handover_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handover not found")
    try:
        handover_mod.acknowledge_handover(
            db, row, incoming_by=user.username, incoming_notes=body.incoming_notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="handover.acknowledge",
        entity_type="handover",
        entity_id=handover_id,
        site_id=site_id,
    )
    db.commit()
    return handover_mod.as_dict(row)
