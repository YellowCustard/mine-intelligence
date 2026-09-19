"""Generator domain service (FP-11). Callers commit unless noted.

Records measured DSE controller readings (write-once, idempotent), derives advisory alarms
(a fault, or a run when the schedule says the genset should be off), and exposes run-hours
health by feeding the latest engine hours into the existing deterministic maintenance
indicator. Advisory only — never controls a genset (brief §15). Measured values are labelled
measured; a value the controller does not report is ``None``, never fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.maintenance.service import assess_health
from minemonitor.storage.models import Event, GeneratorReading

SOURCE = "generator"


def reading_id(site_id: str, asset_id: str, ts: datetime) -> str:
    """Deterministic id: one reading per generator per timestamp — replay is idempotent."""
    stamp = ts.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"gen-{site_id}-{asset_id}-{stamp}"


def _persist_deduped(session: Session, ev: EventV1) -> EventV1 | None:
    """Persist an event under a deterministic id, tolerating a concurrent duplicate."""
    if session.get(Event, ev.event_id) is not None:
        return None
    try:
        with session.begin_nested():
            persist_event(session, ev)
            session.flush()
        return ev
    except IntegrityError:
        return None


def record_reading(
    session: Session,
    *,
    site_id: str,
    asset_id: str,
    ts: datetime,
    running: bool,
    now: datetime | None = None,
    source: str = "dse_sim",
    engine_hours: float | None = None,
    fuel_level_pct: float | None = None,
    expected_running: bool | None = None,
    fault: bool = False,
    fault_code: str | None = None,
) -> tuple[GeneratorReading, bool, list[EventV1]]:
    """Store one generator reading write-once and derive advisory alarms. No commit.

    Returns (row, created, alarms). Idempotent on the deterministic id: a replay returns the
    stored row with ``created=False`` and raises nothing. A newly-recorded reading with a
    ``fault`` raises a ``generator_fault`` alarm; one that is ``running`` while
    ``expected_running`` is False raises ``generator_unexpected_run``. Both are deduped per
    reading and advisory.
    """
    now = now or datetime.now(UTC)
    rid = reading_id(site_id, asset_id, ts)
    existing = session.get(GeneratorReading, rid)
    if existing is not None:
        return existing, False, []
    row = GeneratorReading(
        id=rid,
        site_id=site_id,
        asset_id=asset_id,
        source=source,
        ts=ts,
        engine_hours=engine_hours,
        running=running,
        fuel_level_pct=fuel_level_pct,
        expected_running=expected_running,
        fault=fault,
        fault_code=fault_code,
        created_at=now,
    )
    session.add(row)
    alarms: list[EventV1] = []
    if fault:
        ev = _persist_deduped(
            session,
            EventV1(
                schema="event.v1",
                event_id=f"gen-fault-{rid}",
                site_id=site_id,
                ts=ts,
                type="generator_fault",
                severity="critical",
                asset_id=asset_id,
                zone_id=None,
                source=f"{SOURCE}:{asset_id}",
                summary=f"Generator {asset_id} reported a fault"
                + (f" ({fault_code})" if fault_code else ""),
                detail={"fault_code": fault_code, "provenance": "observed"},
                advisory=True,
                state="open",
            ),
        )
        if ev is not None:
            alarms.append(ev)
    if expected_running is False and running:
        ev = _persist_deduped(
            session,
            EventV1(
                schema="event.v1",
                event_id=f"gen-unexpected-{rid}",
                site_id=site_id,
                ts=ts,
                type="generator_unexpected_run",
                severity="warning",
                asset_id=asset_id,
                zone_id=None,
                source=f"{SOURCE}:{asset_id}",
                summary=f"Generator {asset_id} is running while scheduled off",
                detail={"provenance": "inferred"},
                advisory=True,
                state="open",
            ),
        )
        if ev is not None:
            alarms.append(ev)
    return row, True, alarms


def latest_reading(session: Session, site_id: str, asset_id: str) -> GeneratorReading | None:
    """The most recent reading for a generator (by device time). Site-scoped."""
    stmt = (
        select(GeneratorReading)
        .where(GeneratorReading.site_id == site_id, GeneratorReading.asset_id == asset_id)
        .order_by(GeneratorReading.ts.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def latest_engine_hours(session: Session, site_id: str, asset_id: str) -> float | None:
    """The most recent reported engine hours for a generator, or None if never reported."""
    stmt = (
        select(GeneratorReading.engine_hours)
        .where(
            GeneratorReading.site_id == site_id,
            GeneratorReading.asset_id == asset_id,
            GeneratorReading.engine_hours.is_not(None),
        )
        .order_by(GeneratorReading.ts.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def list_readings(
    session: Session,
    site_id: str,
    *,
    asset_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[GeneratorReading]:
    """List generator readings for a site (always site-scoped), newest first."""
    stmt = select(GeneratorReading).where(GeneratorReading.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(GeneratorReading.asset_id == asset_id)
    if since is not None:
        stmt = stmt.where(GeneratorReading.ts >= since)
    if until is not None:
        stmt = stmt.where(GeneratorReading.ts < until)
    stmt = stmt.order_by(GeneratorReading.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())


def generator_health(
    session: Session,
    site_id: str,
    asset_id: str,
    *,
    component: str = "engine",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run-hours maintenance health for a generator, from its latest reported engine hours.

    Reuses the deterministic maintenance indicator (`assess_health`) — no duplicate logic.
    With no plan or no engine-hours reading, the indicator is ``Unknown`` (never fabricated).
    """
    now = now or datetime.now(UTC)
    hours = latest_engine_hours(session, site_id, asset_id)
    return assess_health(session, site_id, asset_id, component, now=now, current_engine_hours=hours)
