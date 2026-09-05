"""Asset-offline detection — a periodic check, not a per-fix rule.

An asset is *offline* if it was active (its last fix had ignition on) but no
position has arrived for a threshold. That is different from *stationary with
ignition off* (parked), which is normal and raises nothing — the two mean
different things to a supervisor (brief §9). Deduped: one open alarm per asset.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import has_open_event, new_event_id, persist_event
from minemonitor.storage.models import Position

SOURCE = "gnss_offline"


def _aware(dt: datetime) -> datetime:
    """Treat naive timestamps (SQLite) as UTC; Postgres returns aware already."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def detect_offline(
    session: Session,
    site_id: str,
    *,
    now: datetime | None = None,
    threshold_s: float = 600.0,
) -> list[EventV1]:
    """Raise asset_offline events for active assets gone silent. Commits."""
    now = now or datetime.now(UTC)
    latest = (
        select(Position.asset_id, func.max(Position.ts).label("mts"))
        .where(Position.site_id == site_id)
        .group_by(Position.asset_id)
        .subquery()
    )
    rows = session.execute(
        select(Position.asset_id, Position.ts, Position.ignition)
        .join(
            latest,
            and_(Position.asset_id == latest.c.asset_id, Position.ts == latest.c.mts),
        )
        .where(Position.site_id == site_id)
    ).all()

    events: list[EventV1] = []
    for asset_id, ts, ignition in rows:
        age = (now - _aware(ts)).total_seconds()
        # Silent beyond threshold AND last seen active (ignition on) = offline.
        # Ignition off = parked; not an alarm.
        if age <= threshold_s or not ignition:
            continue
        if has_open_event(session, site_id, asset_id, "asset_offline", None):
            continue
        events.append(
            EventV1(
                schema="event.v1",
                event_id=new_event_id(),
                site_id=site_id,
                ts=now,
                type="asset_offline",
                severity="warning",
                asset_id=asset_id,
                zone_id=None,
                source=SOURCE,
                summary=f"{asset_id} offline: no position for {int(age)}s",
                detail={"age_s": age, "threshold_s": threshold_s},
                advisory=True,
                state="open",
            )
        )
    for ev in events:
        persist_event(session, ev)
    session.commit()
    return events
