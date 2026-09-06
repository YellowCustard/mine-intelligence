"""Incident management: raise an incident from an alarm and drive its lifecycle.

Extends the existing alarm queue (``events``) with a full investigation workflow
without touching the raw ``Event`` — the incident links to the alarm and carries
its own lifecycle and append-only timeline. Reads are viewer-level; changes
require supervisor and are audited.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.operations import incidents
from minemonitor.storage.db import get_db
from minemonitor.storage.models import Event, Incident, IncidentNote, User

router = APIRouter(tags=["incidents"])


class IncidentIn(BaseModel):
    summary: str = Field(min_length=1)
    type: str = Field(default="operational")
    severity: str = Field(default="info")
    asset_id: str | None = None
    zone_id: str | None = None
    # Link to an originating alarm. The alarm is never mutated.
    event_id: str | None = None


class TransitionIn(BaseModel):
    to_state: str
    note: str | None = None
    assignee: str | None = None
    resolution: str | None = None
    resolution_category: str | None = None


class NoteIn(BaseModel):
    text: str = Field(min_length=1)


def _incident_dict(row: Incident) -> dict[str, Any]:
    return {
        "incident_id": row.incident_id,
        "site_id": row.site_id,
        "event_id": row.event_id,
        "type": row.type,
        "severity": row.severity,
        "asset_id": row.asset_id,
        "zone_id": row.zone_id,
        "summary": row.summary,
        "state": row.state,
        "assignee": row.assignee,
        "resolution": row.resolution,
        "resolution_category": row.resolution_category,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "resolved_at": row.resolved_at,
        "closed_at": row.closed_at,
    }


def _note_dict(row: IncidentNote) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": row.ts,
        "actor": row.actor,
        "kind": row.kind,
        "from_state": row.from_state,
        "to_state": row.to_state,
        "text": row.text,
    }


@router.get("/sites/{site_id}/incidents", dependencies=[Depends(require_viewer)])
def get_incidents(
    site_id: str,
    state: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List incidents for a site, newest first."""
    rows = incidents.list_incidents(db, site_id, state=state, severity=severity, limit=limit)
    return [_incident_dict(r) for r in rows]


@router.get("/sites/{site_id}/incidents/{incident_id}", dependencies=[Depends(require_viewer)])
def get_incident(site_id: str, incident_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One incident plus its full append-only timeline."""
    row = incidents.get_incident(db, site_id, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return {
        **_incident_dict(row),
        "timeline": [_note_dict(n) for n in incidents.timeline(db, incident_id)],
    }


@router.post("/sites/{site_id}/incidents", status_code=201)
def create_incident(
    site_id: str,
    body: IncidentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Raise an incident, optionally from an existing alarm (which is not mutated)."""
    if body.event_id is not None:
        event = db.get(Event, body.event_id)
        if event is None or event.site_id != site_id:
            raise HTTPException(status_code=404, detail="event not found")
    row = incidents.create_incident(
        db,
        site_id=site_id,
        summary=body.summary,
        type_=body.type,
        severity=body.severity,
        actor=user.username,
        asset_id=body.asset_id,
        zone_id=body.zone_id,
        event_id=body.event_id,
    )
    audit.record(
        db,
        actor=user.username,
        action="incident.create",
        entity_type="incident",
        entity_id=row.incident_id,
        site_id=site_id,
        detail={"event_id": body.event_id, "severity": body.severity},
    )
    db.commit()
    return _incident_dict(row)


@router.post("/sites/{site_id}/incidents/{incident_id}/transition")
def transition_incident(
    site_id: str,
    incident_id: str,
    body: TransitionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Move an incident through its lifecycle. Validated and audited."""
    row = incidents.get_incident(db, site_id, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        incidents.transition_incident(
            db,
            row,
            actor=user.username,
            to_state=body.to_state,
            note=body.note,
            assignee=body.assignee,
            resolution=body.resolution,
            resolution_category=body.resolution_category,
        )
    except incidents.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="incident.transition",
        entity_type="incident",
        entity_id=incident_id,
        site_id=site_id,
        detail={"to_state": body.to_state, "assignee": row.assignee},
    )
    db.commit()
    return _incident_dict(row)


@router.post("/sites/{site_id}/incidents/{incident_id}/notes", status_code=201)
def add_incident_note(
    site_id: str,
    incident_id: str,
    body: NoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Append a free-text note to an incident's timeline. Audited."""
    row = incidents.get_incident(db, site_id, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")
    note = incidents.add_note(db, row, actor=user.username, text=body.text)
    audit.record(
        db,
        actor=user.username,
        action="incident.note",
        entity_type="incident",
        entity_id=incident_id,
        site_id=site_id,
    )
    db.commit()
    return _note_dict(note)


@router.post("/sites/{site_id}/events/{event_id}/incident", status_code=201)
def incident_from_event(
    site_id: str,
    event_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Convenience: raise an incident directly from an alarm, copying its context.

    The alarm is linked, never mutated — the existing ack workflow is untouched.
    """
    event = db.get(Event, event_id)
    if event is None or event.site_id != site_id:
        raise HTTPException(status_code=404, detail="event not found")
    row = incidents.create_incident(
        db,
        site_id=site_id,
        summary=event.summary,
        type_=event.type,
        severity=event.severity,
        actor=user.username,
        asset_id=event.asset_id,
        zone_id=event.zone_id,
        event_id=event.event_id,
    )
    audit.record(
        db,
        actor=user.username,
        action="incident.create",
        entity_type="incident",
        entity_id=row.incident_id,
        site_id=site_id,
        detail={"event_id": event_id, "from": "event"},
    )
    db.commit()
    return _incident_dict(row)
