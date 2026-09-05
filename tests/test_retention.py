"""Per-data-class retention deletion (M6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.retention import run_retention
from minemonitor.storage.models import AuditLog, Base, Event, Position

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield s
    finally:
        s.close()


def _pos(s: Session, asset_id: str, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(Position(site_id="kn-zw-01", asset_id=asset_id, ts=ts, received_at=ts,
                   lat=-17.8, lon=31.0, source="t"))


def _event(s: Session, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(Event(event_id=f"e{days_ago}", site_id="kn-zw-01", ts=ts, type="overspeed",
                severity="warning", source="t", summary="x", advisory=True, state="open"))


def test_old_positions_deleted_recent_kept(session: Session) -> None:
    _pos(session, "A", days_ago=100)  # older than 90d
    _pos(session, "B", days_ago=10)   # recent
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
    audits = session.execute(
        select(AuditLog).where(AuditLog.action == "retention.run")
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].detail["deleted"]["events"] == 1
