"""Persist, dedupe, list and acknowledge events (the unified alarm queue)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.contracts import EventV1
from minemonitor.storage.models import Event


def new_event_id() -> str:
    """A fresh time-ordered ULID for an event."""
    return str(ULID())


def persist_event(session: Session, event: EventV1) -> Event:
    """Write an event row. Caller commits."""
    row = Event(
        event_id=event.event_id,
        site_id=event.site_id,
        ts=event.ts,
        type=event.type,
        severity=event.severity,
        asset_id=event.asset_id,
        zone_id=event.zone_id,
        source=event.source,
        summary=event.summary,
        detail=event.detail,
        evidence=event.evidence,
        advisory=True,
        state=event.state,
        acknowledged_by=event.acknowledged_by,
        acknowledged_at=event.acknowledged_at,
    )
    session.add(row)
    return row


def has_open_event(
    session: Session, site_id: str, asset_id: str, type_: str, zone_id: str | None
) -> bool:
    """True if an unresolved event of this (asset, type, zone) already exists.

    Used to dedupe state-based events (e.g. asset_offline) so an ongoing
    condition raises one alarm, not one per check.
    """
    stmt = select(Event.event_id).where(
        Event.site_id == site_id,
        Event.asset_id == asset_id,
        Event.type == type_,
        Event.state != "resolved",
    )
    if zone_id is None:
        stmt = stmt.where(Event.zone_id.is_(None))
    else:
        stmt = stmt.where(Event.zone_id == zone_id)
    return session.execute(stmt.limit(1)).first() is not None


def list_events(
    session: Session,
    site_id: str,
    *,
    state: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[Event]:
    """List events for a site (always site-scoped), newest first."""
    stmt = select(Event).where(Event.site_id == site_id)
    if state is not None:
        stmt = stmt.where(Event.state == state)
    if severity is not None:
        stmt = stmt.where(Event.severity == severity)
    stmt = stmt.order_by(Event.ts.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def acknowledge_event(
    session: Session, site_id: str, event_id: str, acknowledged_by: str
) -> Event | None:
    """Acknowledge an open event. Returns the row, or None if not found."""
    row = session.get(Event, event_id)
    if row is None or row.site_id != site_id:
        return None
    row.state = "acknowledged"
    row.acknowledged_by = acknowledged_by
    row.acknowledged_at = datetime.now(UTC)
    session.commit()
    return row
