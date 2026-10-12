"""Reachability of polled external feeds (plan G2): one alarm per outage, re-armed on recovery.

A poller calls :func:`record_ok` after a successful fetch and :func:`record_failure` after a
failed one. Once polls have failed for ``offline_after_s``, one ``source_offline`` ``event.v1``
tells the control room the feed is down, so "nothing happened" is never confused with "we cannot
see". The outage's start lives in ``source_status`` (not in memory), so a restart mid-outage
neither loses it nor re-alarms. A second outage after a recovery is a new alarm, whatever state
the first one is in; on recovery, the outage's alarm is annotated with when the feed came back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.storage.models import Event, SourceStatus

SOURCE_OFFLINE: Final = "source_offline"
_MAX_ERROR_CHARS = 300


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _status(session: Session, site_id: str, source: str) -> SourceStatus:
    row = session.get(SourceStatus, (site_id, source))
    if row is None:
        row = SourceStatus(site_id=site_id, source=source)
        session.add(row)
    return row


def _outage_alarm(session: Session, site_id: str, source: str, since: datetime) -> Event | None:
    """The alarm already raised for the outage that began at ``since``, if any."""
    return (
        session.execute(
            select(Event)
            .where(
                Event.site_id == site_id,
                Event.type == SOURCE_OFFLINE,
                Event.source == source,
                Event.ts >= since,
            )
            .order_by(Event.ts.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def record_ok(session: Session, site_id: str, source: str, *, now: datetime) -> None:
    """A successful poll: end any outage and note recovery on its alarm. No commit."""
    row = _status(session, site_id, source)
    if row.failing_since is not None:
        alarm = _outage_alarm(session, site_id, source, _aware(row.failing_since))
        if alarm is not None and "recovered_at" not in (alarm.detail or {}):
            # Annotate rather than resolve: an outage nobody has acknowledged yet stays in the
            # queue (it was a blind window), but it must not read as still ongoing.
            down_min = int((now - _aware(row.failing_since)).total_seconds() // 60)
            alarm.detail = {**(alarm.detail or {}), "recovered_at": now.isoformat()}
            alarm.summary = f"{alarm.summary} (restored after {down_min} min)"
    row.last_ok_at = now
    row.failing_since = None
    row.last_error = None


def record_failure(
    session: Session,
    site_id: str,
    source: str,
    *,
    now: datetime,
    error: str,
    offline_after_s: int,
    label: str,
) -> EventV1 | None:
    """A failed poll: track the outage and raise ``source_offline`` once it lasts. No commit.

    ``label`` names the feed for a person (e.g. "Gate feed dahua_gate"). Returns the alarm when
    one is raised on this call, else None.
    """
    row = _status(session, site_id, source)
    if row.failing_since is None:
        row.failing_since = now
    row.last_error = error[:_MAX_ERROR_CHARS]
    since = _aware(row.failing_since)
    down_s = (now - since).total_seconds()
    if down_s < offline_after_s or _outage_alarm(session, site_id, source, since) is not None:
        return None
    alarm = EventV1(
        schema="event.v1",
        event_id=new_event_id(),
        site_id=site_id,
        ts=now,
        type=SOURCE_OFFLINE,
        severity="warning",
        asset_id=None,
        zone_id=None,
        source=source,
        summary=f"{label} unreachable for {int(down_s // 60)} min: its events are not arriving",
        detail={
            "failing_since": since.isoformat(),
            "down_s": down_s,
            "offline_after_s": offline_after_s,
            "last_ok_at": _aware(row.last_ok_at).isoformat() if row.last_ok_at else None,
            "last_error": row.last_error,
            "provenance": "observed",
        },
        advisory=True,
        state="open",
    )
    persist_event(session, alarm)
    return alarm
