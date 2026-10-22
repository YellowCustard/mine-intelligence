"""Retention deletes old alarms that other rows still point at (brief §4).

``notifications`` and ``incidents`` both carry a foreign key to ``events``. Deleting an
alarm past the events window while one of them still points at it violates that key, and
because retention runs as one transaction, the whole run rolls back: positions, metrics
and every other class stop being deleted from the first night an alarm that was notified
or turned into an incident ages out. SQLite does not enforce foreign keys unless asked,
so these tests switch enforcement on, and also run against PostgreSQL when
``MM_TEST_DATABASE_URL`` points at one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.retention import run_retention
from minemonitor.storage.models import (
    Base,
    Event,
    Incident,
    IncidentNote,
    Notification,
    Position,
    Site,
)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
_PG = os.environ.get("MM_TEST_DATABASE_URL", "")
SITE = "kn-zw-01"


def _sqlite() -> Engine:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_fks(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture(params=["sqlite", "postgresql"])
def session(request: pytest.FixtureRequest) -> Iterator[Session]:
    if request.param == "postgresql":
        if not _PG.startswith("postgresql"):
            pytest.skip("set MM_TEST_DATABASE_URL to a PostgreSQL URL to run")
        engine = create_engine(_PG, future=True)
        Base.metadata.drop_all(engine)
    else:
        engine = _sqlite()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        if request.param == "postgresql":
            Base.metadata.drop_all(engine)
        engine.dispose()


def _alarm(s: Session, event_id: str, days_ago: float) -> None:
    s.add(
        Event(
            event_id=event_id,
            site_id=SITE,
            ts=_NOW - timedelta(days=days_ago),
            type="zone_breach",
            severity="critical",
            source="gnss_geofence",
            summary="LV-07 entered R1",
            advisory=True,
            state="open",
        )
    )
    s.flush()


def _notified(s: Session, event_id: str, days_ago: float) -> None:
    at = _NOW - timedelta(days=days_ago)
    s.add(
        Notification(
            id=f"n-{event_id}",
            site_id=SITE,
            event_id=event_id,
            channel="email",
            target="control-room@mine.example",
            state="sent",
            attempts=1,
            created_at=at,
            next_attempt_at=at,
            sent_at=at,
        )
    )


def _incident(s: Session, iid: str, event_id: str, *, closed_days_ago: float | None) -> None:
    opened = _NOW - timedelta(days=500)
    closed = None if closed_days_ago is None else _NOW - timedelta(days=closed_days_ago)
    s.add(
        Incident(
            incident_id=iid,
            site_id=SITE,
            event_id=event_id,
            type="zone_breach",
            severity="critical",
            summary="magazine entry",
            state="open" if closed is None else "closed",
            created_by="sup",
            created_at=opened,
            updated_at=opened,
            closed_at=closed,
        )
    )
    s.flush()
    s.add(
        IncidentNote(
            id=f"note-{iid}",
            incident_id=iid,
            site_id=SITE,
            ts=opened,
            actor="sup",
            kind="note",
            text="checked",
        )
    )


def _old_fix(s: Session) -> None:
    ts = _NOW - timedelta(days=100)
    s.add(
        Position(
            site_id=SITE, asset_id="HT-102", ts=ts, received_at=ts, lat=-17.8, lon=31.0, source="t"
        )
    )


def _run(s: Session) -> dict[str, int]:
    return run_retention(
        s, now=_NOW, positions_days=90, metrics_days=365, events_days=365, annotations_days=365
    )


def _ids(s: Session, col: Any) -> list[str]:
    return sorted(s.execute(select(col)).scalars().all())


def test_old_notified_alarm_is_deleted_with_its_notifications(session: Session) -> None:
    _alarm(session, "e-old", days_ago=400)
    _notified(session, "e-old", days_ago=400)
    _alarm(session, "e-new", days_ago=5)
    _notified(session, "e-new", days_ago=5)
    _old_fix(session)
    session.commit()

    deleted = _run(session)

    assert deleted["events"] == 1
    assert deleted["notifications"] == 1
    assert deleted["positions"] == 1  # the rest of the run is not rolled back with it
    assert _ids(session, Event.event_id) == ["e-new"]
    assert _ids(session, Notification.event_id) == ["e-new"]
    assert session.execute(select(func.count()).select_from(Position)).scalar_one() == 0


def test_old_alarm_behind_an_open_incident_is_kept(session: Session) -> None:
    _alarm(session, "e-incident", days_ago=400)
    _notified(session, "e-incident", days_ago=400)
    _incident(session, "inc-open", "e-incident", closed_days_ago=None)
    _alarm(session, "e-plain", days_ago=400)
    _old_fix(session)
    session.commit()

    deleted = _run(session)

    # The incident is live work: its alarm and that alarm's delivery record stay.
    assert _ids(session, Event.event_id) == ["e-incident"]
    assert _ids(session, Notification.event_id) == ["e-incident"]
    assert _ids(session, Incident.incident_id) == ["inc-open"]
    assert deleted["events"] == 1
    assert deleted["notifications"] == 0
    assert deleted["positions"] == 1


def test_alarm_kept_while_its_closed_incident_is_in_window(session: Session) -> None:
    _alarm(session, "e-incident", days_ago=400)
    _incident(session, "inc-recent", "e-incident", closed_days_ago=100)
    session.commit()

    deleted = _run(session)

    assert deleted["events"] == 0
    assert deleted["incidents"] == 0
    assert _ids(session, Event.event_id) == ["e-incident"]


def test_closed_incident_and_its_old_alarm_go_in_the_same_run(session: Session) -> None:
    _alarm(session, "e-incident", days_ago=800)
    _notified(session, "e-incident", days_ago=800)
    _incident(session, "inc-closed", "e-incident", closed_days_ago=400)
    session.commit()

    deleted = _run(session)

    assert deleted["incidents"] == 1
    assert deleted["incident_notes"] == 1
    assert deleted["events"] == 1
    assert deleted["notifications"] == 1
    assert _ids(session, Event.event_id) == []
    assert _ids(session, Incident.incident_id) == []
    assert _ids(session, Notification.id) == []


def test_old_records_go_with_their_children_without_rolling_back_the_run(
    session: Session,
) -> None:
    """Lab corrections and dispatch assignments point at their parent by foreign key.

    An old lab result with a recent correction, and an old ended job with its assignment, are
    deleted with those children; the rest of the run (here, positions) still happens. The
    self-referencing work-order and generator deletes run on PostgreSQL too.
    """
    from minemonitor.storage.models import (
        DispatchAssignment,
        DispatchJob,
        GeneratorReading,
        LaboratoryCorrection,
        LaboratoryResult,
        WorkOrder,
    )
    from tests.test_retention import _gen, _seed_records

    _seed_records(session, "old", 2600)
    _seed_records(session, "new", 30)
    _gen(session, "GEN-1", 200)
    _gen(session, "GEN-1", 5)
    _old_fix(session)
    session.commit()

    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=90,
        metrics_days=365,
        events_days=365,
        records_days=2555,
        readings_days=90,
    )

    assert deleted["positions"] == 1
    assert deleted["laboratory_corrections"] == 1
    assert _ids(session, LaboratoryResult.id) == ["lab-new"]
    assert _ids(session, LaboratoryCorrection.id) == ["labc-new"]
    assert _ids(session, DispatchJob.id) == ["job-new"]
    assert _ids(session, DispatchAssignment.id) == ["da-new"]
    assert _ids(session, WorkOrder.id) == ["wo-new"]
    assert _ids(session, GeneratorReading.id) == ["gen-GEN-1-5"]
