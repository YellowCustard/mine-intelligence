"""The alarm queue: list events (by severity/state) and acknowledge them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor.events.repository import acknowledge_event, list_events
from minemonitor.storage.db import get_db

router = APIRouter(tags=["events"])


class AckIn(BaseModel):
    acknowledged_by: str = Field(min_length=1)


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


@router.get("/sites/{site_id}/events")
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
    site_id: str, event_id: str, body: AckIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Acknowledge an event."""
    row = acknowledge_event(db, site_id, event_id, body.acknowledged_by)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    return _to_dict(row)
