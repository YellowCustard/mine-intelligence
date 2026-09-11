"""Per-data-class retention deletion (M6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.retention import run_retention
from minemonitor.storage.models import (
    AuditLog,
    Base,
    DelayClassification,
    Event,
    Incident,
    IncidentNote,
    Position,
    ShiftHandover,
)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield s
    finally:
        s.close()


def _pos(s: Session, asset_id: str, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(
        Position(
            site_id="kn-zw-01",
            asset_id=asset_id,
            ts=ts,
            received_at=ts,
            lat=-17.8,
            lon=31.0,
            source="t",
        )
    )


def _event(s: Session, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(
        Event(
            event_id=f"e{days_ago}",
            site_id="kn-zw-01",
            ts=ts,
            type="overspeed",
            severity="warning",
            source="t",
            summary="x",
            advisory=True,
            state="open",
        )
    )


def test_old_positions_deleted_recent_kept(session: Session) -> None:
    _pos(session, "A", days_ago=100)  # older than 90d
    _pos(session, "B", days_ago=10)  # recent
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["positions"] == 1
    remaining = session.execute(select(func.count()).select_from(Position)).scalar_one()
    assert remaining == 1  # the recent one survives


def test_zero_days_keeps_forever(session: Session) -> None:
    _pos(session, "A", days_ago=1000)
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=0, metrics_days=0, events_days=0)
    assert deleted["positions"] == 0
    assert session.execute(select(func.count()).select_from(Position)).scalar_one() == 1


def test_events_retention_and_audit_written(session: Session) -> None:
    _event(session, days_ago=400)
    _event(session, days_ago=5)
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["events"] == 1
    # A retention run is itself audited (accountable deletion, brief §4).
    audits = (
        session.execute(select(AuditLog).where(AuditLog.action == "retention.run")).scalars().all()
    )
    assert len(audits) == 1
    assert audits[0].detail["deleted"]["events"] == 1


def _audit(s: Session, days_ago: float) -> None:
    s.add(
        AuditLog(
            id=f"a{days_ago}",
            ts=_NOW - timedelta(days=days_ago),
            actor="system",
            action="test.event",
            entity_type="test",
        )
    )


def test_audit_log_is_pruned_but_run_record_survives(session: Session) -> None:
    _audit(session, days_ago=800)  # older than the 730d audit window
    _audit(session, days_ago=5)  # recent
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, audit_days=730
    )
    assert deleted["audit_log"] == 1  # only the old audit row
    # Survivors: the recent audit row + the run's own retention.run record.
    remaining = session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert remaining == 2


def test_audit_zero_days_keeps_forever(session: Session) -> None:
    _audit(session, days_ago=5000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=0, metrics_days=0, events_days=0, audit_days=0
    )
    assert deleted["audit_log"] == 0


# --- operational annotations -------------------------------------------------


def _incident(
    s: Session, iid: str, *, closed_days_ago: float | None, created_days_ago: float
) -> None:
    created = _NOW - timedelta(days=created_days_ago)
    closed = None if closed_days_ago is None else _NOW - timedelta(days=closed_days_ago)
    s.add(
        Incident(
            incident_id=iid,
            site_id="kn-zw-01",
            type="zone_breach",
            severity="critical",
            summary="x",
            state="closed" if closed else "open",
            created_by="sup",
            created_at=created,
            updated_at=created,
            closed_at=closed,
        )
    )
    s.add(
        IncidentNote(
            id=f"n-{iid}",
            incident_id=iid,
            site_id="kn-zw-01",
            ts=created,
            actor="sup",
            kind="note",
            text="did a thing",
        )
    )


def _delay(s: Session, did: str, end_days_ago: float) -> None:
    end = _NOW - timedelta(days=end_days_ago)
    s.add(
        DelayClassification(
            id=did,
            site_id="kn-zw-01",
            category="loader_unavailable",
            start_ts=end - timedelta(hours=1),
            end_ts=end,
            created_by="sup",
            created_at=end,
        )
    )


def _handover(s: Session, hid: str, created_days_ago: float) -> None:
    s.add(
        ShiftHandover(
            id=hid,
            site_id="kn-zw-01",
            shift_id="kn-zw-01:2026-01-01:day",
            summary={},
            outgoing_by="sup",
            created_at=_NOW - timedelta(days=created_days_ago),
        )
    )


def test_annotations_pruned_by_window(session: Session) -> None:
    # Closed-and-old incident -> deleted with its note; open incident of any age
    # and a recently-closed one survive.
    _incident(session, "old-closed", closed_days_ago=400, created_days_ago=410)
    _incident(session, "old-open", closed_days_ago=None, created_days_ago=999)
    _incident(session, "recent-closed", closed_days_ago=10, created_days_ago=20)
    _delay(session, "old-delay", end_days_ago=400)
    _delay(session, "recent-delay", end_days_ago=10)
    _handover(session, "old-h", created_days_ago=400)
    _handover(session, "recent-h", created_days_ago=10)
    session.commit()

    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=0,
        metrics_days=0,
        events_days=0,
        annotations_days=365,
    )
    assert deleted["incidents"] == 1 and deleted["incident_notes"] == 1
    assert deleted["delay_classifications"] == 1
    assert deleted["shift_handovers"] == 1

    surviving = {i for (i,) in session.execute(select(Incident.incident_id)).all()}
    assert surviving == {"old-open", "recent-closed"}  # open work never age-deleted
    assert session.execute(select(func.count()).select_from(IncidentNote)).scalar_one() == 2


def test_annotations_zero_days_keeps_forever(session: Session) -> None:
    _incident(session, "old-closed", closed_days_ago=9999, created_days_ago=9999)
    _delay(session, "old-delay", end_days_ago=9999)
    _handover(session, "old-h", created_days_ago=9999)
    session.commit()
    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=0,
        metrics_days=0,
        events_days=0,
        annotations_days=0,
    )
    assert deleted["incidents"] == 0
    assert deleted["delay_classifications"] == 0
    assert deleted["shift_handovers"] == 0
