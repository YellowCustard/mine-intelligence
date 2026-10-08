"""Unit tests for asset-offline detection (offline vs parked, deduped)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.rules.offline import detect_offline
from minemonitor.storage.models import Base, Position

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


def _pos(session: Session, asset_id: str, minutes_ago: float, ignition: bool) -> None:
    ts = _NOW - timedelta(minutes=minutes_ago)
    session.add(
        Position(
            site_id="kn-zw-01",
            asset_id=asset_id,
            ts=ts,
            received_at=ts,
            lat=-17.8,
            lon=31.0,
            speed_kph=0.0,
            ignition=ignition,
            source="test",
        )
    )
    session.commit()


def test_active_asset_gone_silent_is_offline(session: Session) -> None:
    _pos(session, "HT-101", minutes_ago=20, ignition=True)  # last seen active, 20m ago
    events = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)
    assert len(events) == 1
    assert events[0].type == "asset_offline"
    assert events[0].asset_id == "HT-101"


def test_parked_asset_is_not_offline(session: Session) -> None:
    _pos(session, "HT-102", minutes_ago=20, ignition=False)  # parked, engine off
    events = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)
    assert events == []


def test_recent_asset_is_not_offline(session: Session) -> None:
    _pos(session, "HT-103", minutes_ago=2, ignition=True)  # seen 2m ago
    events = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)
    assert events == []


def test_offline_is_deduped(session: Session) -> None:
    _pos(session, "HT-101", minutes_ago=20, ignition=True)
    first = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)
    second = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)
    assert len(first) == 1
    assert second == []  # already an open alarm


# -- re-arming: a second outage is a new alarm --------------------------------------------


def test_second_outage_after_recovery_raises_again(session: Session) -> None:
    from minemonitor.events.repository import acknowledge_event

    _pos(session, "HT-101", minutes_ago=120, ignition=True)
    first = detect_offline(session, "kn-zw-01", now=_NOW - timedelta(minutes=100), threshold_s=600)
    assert len(first) == 1
    acknowledge_event(session, "kn-zw-01", first[0].event_id, "sup")
    _pos(session, "HT-101", minutes_ago=60, ignition=True)  # back online
    later = _NOW - timedelta(minutes=58)
    assert detect_offline(session, "kn-zw-01", now=later, threshold_s=600) == []
    again = detect_offline(session, "kn-zw-01", now=_NOW, threshold_s=600)  # silent 60 min
    assert len(again) == 1 and again[0].asset_id == "HT-101"


def test_unacknowledged_alarm_does_not_mute_the_next_outage(session: Session) -> None:
    _pos(session, "HT-101", minutes_ago=120, ignition=True)
    assert len(detect_offline(session, "kn-zw-01", now=_NOW - timedelta(minutes=100))) == 1
    _pos(session, "HT-101", minutes_ago=60, ignition=True)
    assert len(detect_offline(session, "kn-zw-01", now=_NOW)) == 1


def test_one_outage_is_one_alarm_even_after_ack(session: Session) -> None:
    from minemonitor.events.repository import acknowledge_event

    _pos(session, "HT-101", minutes_ago=60, ignition=True)
    first = detect_offline(session, "kn-zw-01", now=_NOW - timedelta(minutes=40))
    assert len(first) == 1
    acknowledge_event(session, "kn-zw-01", first[0].event_id, "sup")
    assert detect_offline(session, "kn-zw-01", now=_NOW) == []  # same silence, no new fix
