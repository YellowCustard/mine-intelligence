"""The ingestor's maintenance tick: every site (audit H5), each step isolated (audit H6)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.breaches.service import record_breach
from minemonitor.config import get_settings
from minemonitor.contracts import AssetPositionV1
from minemonitor.ingest.mqtt import MqttIngestor
from minemonitor.storage.models import Event, RecomputeCursor, Site
from minemonitor.storage.repositories import insert_position
from tests.conftest import VIEWER, make_client

SECOND = "zz-zw-02"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _OneTick(threading.Event):
    """A stop event that lets the maintenance loop run exactly ``n`` ticks, synchronously."""

    def __init__(self, n: int = 1) -> None:
        super().__init__()
        self._left = n

    def wait(self, timeout: float | None = None) -> bool:
        self._left -= 1
        return self._left < 0


def _tick(ing: MqttIngestor, n: int = 1) -> None:
    ing._stop = _OneTick(n)
    ing._offline_loop()


@pytest.fixture
def ingestor(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> MqttIngestor:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    return MqttIngestor(write_retry_s=0.0)


def _silent_truck(db: Session, site_id: str, asset_id: str) -> None:
    """A truck that was working (ignition on) and stopped reporting 30 minutes ago."""
    last = datetime.now(UTC) - timedelta(minutes=30)
    for i in range(3):
        insert_position(
            db,
            AssetPositionV1(
                site_id=site_id,
                asset_id=asset_id,
                ts=last - timedelta(seconds=2 - i),
                received_at=last,
                lat=-17.8252,
                lon=31.0335,
                speed_kph=20.0,
                ignition=True,
                source="test",
            ),
            commit=False,
        )
    db.commit()


def _offline_alarms(db: Session) -> set[tuple[str, str]]:
    db.expire_all()
    rows = db.execute(select(Event.site_id, Event.asset_id).where(Event.type == "asset_offline"))
    return {(s, a) for s, a in rows}


def _cycles_refreshed(db: Session, site_id: str, asset_id: str) -> bool:
    db.expire_all()
    cur = db.get(RecomputeCursor, (site_id, asset_id))
    return cur is not None and cur.through_ts is not None


def test_every_site_gets_alarms_and_analytics(db_session: Session, ingestor: MqttIngestor) -> None:
    # H5: a second site's assets used to get no offline alarms, cycles or metrics.
    db_session.add(Site(site_id=SECOND, name="Second", timezone="Africa/Harare"))
    db_session.commit()
    _silent_truck(db_session, "kn-zw-01", "HT-102")
    _silent_truck(db_session, SECOND, "HT-901")

    _tick(ingestor)

    assert _offline_alarms(db_session) == {("kn-zw-01", "HT-102"), (SECOND, "HT-901")}
    assert _cycles_refreshed(db_session, "kn-zw-01", "HT-102")
    assert _cycles_refreshed(db_session, SECOND, "HT-901")


def _overdue_breach(db: Session, site_id: str) -> None:
    """A breach found 30 hours ago: its 24-hour regulator notice is overdue and unsent."""
    record_breach(
        db,
        site_id=site_id,
        discovered_at=datetime.now(UTC) - timedelta(hours=30),
        occurred_at=None,
        summary="Operator export emailed to the wrong address",
        data_classes=["operator_records"],
        operator_ids=[],
        notify_individuals=False,
        actor="admin",
    )
    db.commit()


def test_every_site_gets_its_breach_notice_alarms(
    db_session: Session, ingestor: MqttIngestor
) -> None:
    # C-1's deadline check joined the tick for the default site only; it is per site too.
    db_session.add(Site(site_id=SECOND, name="Second", timezone="Africa/Harare"))
    db_session.commit()
    _overdue_breach(db_session, "kn-zw-01")
    _overdue_breach(db_session, SECOND)

    _tick(ingestor)

    db_session.expire_all()
    rows = db_session.execute(
        select(Event.site_id, Event.severity).where(Event.type == "breach_notice_due")
    )
    assert sorted(rows) == [("kn-zw-01", "critical"), (SECOND, "critical")]


def test_default_site_runs_even_without_a_sites_row(
    db_session: Session, ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MM_DEFAULT_SITE_ID", "yy-zw-09")
    get_settings.cache_clear()
    ing = MqttIngestor(write_retry_s=0.0)
    _silent_truck(db_session, "yy-zw-09", "HT-950")
    _tick(ing)
    assert ("yy-zw-09", "HT-950") in _offline_alarms(db_session)


def test_a_failing_step_does_not_skip_the_rest(
    db_session: Session, ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H6: one step raising used to abort the tick, so every later step (cycles,
    # notifications, retention) silently stopped for as long as it kept failing.
    db_session.add(Site(site_id=SECOND, name="Second", timezone="Africa/Harare"))
    db_session.commit()
    _silent_truck(db_session, "kn-zw-01", "HT-102")
    _silent_truck(db_session, SECOND, "HT-901")

    def boom(session: Session, site_id: str, **kwargs: object) -> list[object]:
        if site_id == "kn-zw-01":
            raise RuntimeError("occupancy broke")
        return []

    monkeypatch.setattr("minemonitor.rules.occupancy.detect_zone_occupancy", boom)
    _tick(ingestor)

    assert _offline_alarms(db_session) == {("kn-zw-01", "HT-102"), (SECOND, "HT-901")}
    assert _cycles_refreshed(db_session, "kn-zw-01", "HT-102")
    assert _cycles_refreshed(db_session, SECOND, "HT-901")


def test_a_failing_step_shows_in_system_health_until_it_recovers(
    db_session: Session, ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(session: Session, site_id: str) -> dict[str, int]:
        raise RuntimeError("cycles broke")

    monkeypatch.setattr("minemonitor.cycles.recompute.refresh", boom)
    _tick(ingestor)
    client: TestClient = make_client(db_session, VIEWER)
    body = client.get("/sites/kn-zw-01/system-health").json()
    assert body["app_plane"]["failing_steps"] == ["cycles"]
    assert body["verdict"] == "platform_degraded"

    monkeypatch.undo()
    _tick(ingestor)
    body = client.get("/sites/kn-zw-01/system-health").json()
    assert body["app_plane"]["failing_steps"] == []


def test_a_failing_step_on_one_site_does_not_degrade_another(
    db_session: Session, ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(Site(site_id=SECOND, name="Second", timezone="Africa/Harare"))
    db_session.commit()

    def boom(session: Session, site_id: str, **kwargs: object) -> list[object]:
        if site_id == SECOND:
            raise RuntimeError("occupancy broke")
        return []

    monkeypatch.setattr("minemonitor.rules.occupancy.detect_zone_occupancy", boom)
    _tick(ingestor)
    client = make_client(db_session, VIEWER)
    assert client.get("/sites/kn-zw-01/system-health").json()["app_plane"]["failing_steps"] == []


def test_failed_retention_is_retried_next_tick_without_blocking_the_others(
    db_session: Session, ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _silent_truck(db_session, "kn-zw-01", "HT-102")
    calls: list[int] = []

    def boom(session: Session) -> dict[str, int]:
        calls.append(1)
        raise RuntimeError("retention broke")

    monkeypatch.setattr("minemonitor.retention.run_from_config", boom)
    _tick(ingestor, n=2)
    assert len(calls) == 2  # not marked done after a failure
    assert _cycles_refreshed(db_session, "kn-zw-01", "HT-102")

    monkeypatch.undo()
    _tick(ingestor, n=2)
    assert ingestor._last_retention > 0  # done: not due again for a day
