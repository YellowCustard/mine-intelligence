"""The alarm queue: list events (by severity/state) and acknowledge them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.events.repository import acknowledge_event, list_events
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User

router = APIRouter(tags=["events"])


class AckIn(BaseModel):
    # Optional note; the acknowledger is the authenticated user, not this field.
    note: str | None = Field(default=None)


def _to_dict(row: Any) -> dict[str, Any]:
    return {
        "schema": "event.v1",
        "event_id": row.event_id,
        "site_id": row.site_id,
        "ts": row.ts,
        "type": row.type,
        "severity": row.severity,
        "asset_id": row.asset_id,
        "zone_id": row.zone_id,
        "source": row.source,
        "summary": row.summary,
        "detail": row.detail,
        "evidence": row.evidence,
        "advisory": row.advisory,
        "state": row.state,
        "acknowledged_by": row.acknowledged_by,
        "acknowledged_at": row.acknowledged_at,
    }


@router.get("/sites/{site_id}/events", dependencies=[Depends(require_viewer)])
def get_events(
    site_id: str,
    state: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List events for a site, newest first. Grouped by severity in the UI."""
    rows = list_events(db, site_id, state=state, severity=severity, limit=limit)
    return [_to_dict(r) for r in rows]


@router.post("/sites/{site_id}/events/{event_id}/ack")
def ack_event(
    site_id: str,
    event_id: str,
    body: AckIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Acknowledge an event. The acknowledger is the authenticated user."""
    row = acknowledge_event(db, site_id, event_id, user.username)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    audit.record(
        db,
        actor=user.username,
        action="event.ack",
        entity_type="event",
        entity_id=event_id,
        site_id=site_id,
        detail={"note": body.note},
    )
    db.commit()
    return _to_dict(row)
