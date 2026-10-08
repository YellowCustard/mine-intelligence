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
    """Write an event row and queue any notifications for it. Caller commits.

    Notification rows land in the same transaction as the event (store-and-forward,
    brief §3), and enqueue is a no-op unless notifications are configured — so this is
    invisible to callers that do not use them.
    """
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
    # Import locally to avoid a package import cycle (notifications -> models -> ...).
    from minemonitor.notifications.outbox import enqueue_for_event

    enqueue_for_event(session, event)
    return row


def alarmed_since(
    session: Session,
    site_id: str,
    type_: str,
    since: datetime,
    *,
    asset_id: str | None = None,
    source: str | None = None,
) -> bool:
    """True if an event of this type (for this asset or source) was raised at or after ``since``.

    Dedupes condition-based alarms (``asset_offline``, ``camera_offline``) **per episode**:
    ``since`` is when the current condition began (the last fix, the last health report), so an
    ongoing condition raises one alarm while a new episode after a recovery raises a new one.
    Keying on "not resolved" instead muted every later episode, because nothing resolves events;
    an acknowledgement must not silence the next outage.
    """
    stmt = select(Event.event_id).where(
        Event.site_id == site_id, Event.type == type_, Event.ts >= since
    )
    if asset_id is not None:
        stmt = stmt.where(Event.asset_id == asset_id)
    if source is not None:
        stmt = stmt.where(Event.source == source)
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
