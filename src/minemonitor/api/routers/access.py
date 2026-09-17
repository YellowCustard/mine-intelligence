"""Access-control API (FP-07) — ingest gate access events, read them, set operator status.

Mine Monitor is the record + intelligence layer; the gate hardware enforces. Ingest applies
Mine Monitor's authorisation rules and raises a critical ``access_denied`` ``event.v1`` when
the gate granted entry to someone those rules would reject. **No biometric data ever** — the
ingest body has no template/image field, and a payload carrying one is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.access import service
from minemonitor.auth.deps import require_admin, require_supervisor, require_viewer
from minemonitor.ingest.adapters.access_sim import normalise_access_event
from minemonitor.storage.db import get_db
from minemonitor.storage.models import AccessEvent, User

router = APIRouter(tags=["access"])


class AccessEventIn(BaseModel):
    """A raw gate access event. No biometric field exists here (brief §4)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    time: str = Field(min_length=1)  # ISO-8601 with offset
    decision: str
    credential_ref: str | None = None
    operator_ref: str | None = None
    reason: str | None = None
    search_selected: bool = False
    search_completed: bool | None = None


class OperatorAccessStatusIn(BaseModel):
    suspended: bool | None = None
    inducted: bool | None = None


class SearchCompleteIn(BaseModel):
    metal_detected: bool | None = None


def _event_dict(e: AccessEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "site_id": e.site_id,
        "ts": e.ts,
        "source_system": e.source_system,
        "gate_id": e.gate_id,
        "credential_ref": e.credential_ref,
        "operator_ref": e.operator_ref,
        "decision": e.decision,
        "reason": e.reason,
        "search_selected": e.search_selected,
        "search_completed": e.search_completed,
        "metal_detected": e.metal_detected,
        "advisory": True,
    }


@router.post("/sites/{site_id}/access/events", status_code=201)
def ingest_access_event(
    site_id: str,
    body: AccessEventIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Ingest one gate access event (supervisor; audited). Idempotent per source event.

    Runs authorisation; a critical ``access_denied`` alarm is raised when the gate granted
    entry to someone the rules reject. Returns the stored event plus whether an alarm fired.
    """
    try:
        kwargs = normalise_access_event(body.model_dump())
        row, created, alarm = service.ingest_access_event(
            db, site_id, now=datetime.now(UTC), **kwargs
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="access.event.ingest",
        entity_type="access_event",
        entity_id=row.id,
        site_id=site_id,
        detail={"gate_id": row.gate_id, "decision": row.decision, "alarm": alarm is not None},
    )
    db.commit()
    return {"event": _event_dict(row), "created": created, "alarm_raised": alarm is not None}


@router.get("/sites/{site_id}/access/events")
def list_access_events(
    site_id: str,
    gate_id: str | None = None,
    decision: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """List access events for a site (viewer; newest first). Site-scoped."""
    rows = service.list_access_events(db, site_id, gate_id=gate_id, decision=decision)
    return [_event_dict(e) for e in rows]


@router.post("/sites/{site_id}/access/events/{access_event_id}/search")
def complete_search(
    site_id: str,
    access_event_id: str,
    body: SearchCompleteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Record a completed physical search on a passage (supervisor; audited).

    Marks the passage searched (clearing any missed-search escalation) and records the
    metal-detector outcome; a positive detection raises a critical ``metal_detected`` alarm.
    """
    row, alarm = service.complete_search(
        db, site_id, access_event_id, metal_detected=body.metal_detected, now=datetime.now(UTC)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "access event not found")
    audit.record(
        db,
        actor=user.username,
        action="access.search.complete",
        entity_type="access_event",
        entity_id=row.id,
        site_id=site_id,
        detail={"gate_id": row.gate_id, "metal_detected": row.metal_detected},
    )
    db.commit()
    return {"event": _event_dict(row), "alarm_raised": alarm is not None}


@router.post("/sites/{site_id}/operators/{operator_id}/access-status")
def set_operator_access_status(
    site_id: str,
    operator_id: str,
    body: OperatorAccessStatusIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Set an operator's access status — suspended / inducted (admin; audited)."""
    op = service.set_operator_access_status(
        db, site_id, operator_id, suspended=body.suspended, inducted=body.inducted
    )
    if op is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "operator not found")
    audit.record(
        db,
        actor=admin.username,
        action="access.operator.status",
        entity_type="operator",
        entity_id=operator_id,
        site_id=site_id,
        detail={"suspended": op.suspended, "inducted": op.inducted},
    )
    db.commit()
    return {"operator_id": op.operator_id, "suspended": op.suspended, "inducted": op.inducted}
