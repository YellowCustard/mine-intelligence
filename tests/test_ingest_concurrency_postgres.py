"""Concurrent ingest for one asset raises one breach per entry, not two (audit M9).

HTTP ingest runs requests in parallel, and MQTT and HTTP can carry the same asset at
once. Each request reads the asset's zone state, steps it, and writes it back; two
fixes processed side by side both read the state before either commits, so both see
"one more inside fix confirms entry" and both raise the breach.

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL: the race needs two real
connections (SQLite serialises writers).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor import pipeline
from minemonitor.contracts import EventV1
from minemonitor.contracts.position import AssetPositionV1
from minemonitor.ingest.service import PositionIngest, store_and_process
from minemonitor.storage.models import Asset, Base, Event, Site
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)

_MAG = (-17.8240, 31.0290)
_FAR = (_MAG[0] + 0.01, _MAG[1])
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(_DB, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # As in production (storage/db.py): nothing reaches the database before commit.
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with make() as s:
        s.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
        s.commit()
        s.add(Asset(asset_id="LV-07", site_id="kn-zw-01", asset_class="light_vehicle"))
        s.add(Asset(asset_id="HT-102", site_id="kn-zw-01", asset_class="haul_truck"))
        upsert_zone(
            s,
            site_id="kn-zw-01",
            zone_id="r1-explosives-magazine",
            name="R1 Explosives Magazine",
            kind="restricted",
            geometry=box_polygon(*_MAG, 40),
            rules={"authorized_classes": [], "severity": "critical"},
        )
        s.commit()
    try:
        yield make
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _fix(i: int, at: tuple[float, float], asset_id: str = "LV-07") -> PositionIngest:
    return PositionIngest.model_validate(
        {
            "schema": "asset.position.v1",
            "site_id": "kn-zw-01",
            "asset_id": asset_id,
            "ts": T0 + timedelta(seconds=i),
            "lat": at[0],
            "lon": at[1],
            "speed_kph": 10.0,
            "ignition": True,
            "source": "test",
        }
    )


def test_two_fixes_processed_at_once_raise_one_breach(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with factory() as s:
        store_and_process(s, _fix(0, _FAR))
        store_and_process(s, _fix(1, _MAG))  # one inside fix: entry needs one more

    # Hold the first request's transaction open after it has processed its fix, the way
    # a slow request would, while the second one runs.
    real = pipeline.process_position
    held = threading.Event()
    release = threading.Event()

    def slow(session: Session, pos: AssetPositionV1, created: bool) -> list[EventV1]:
        events = real(session, pos, created)
        if pos.ts == T0 + timedelta(seconds=2):
            held.set()
            release.wait(10)
        return events

    monkeypatch.setattr(pipeline, "process_position", slow)
    errors: list[BaseException] = []

    def ingest(i: int) -> None:
        try:
            with factory() as s:
                store_and_process(s, _fix(i, _MAG))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    first = threading.Thread(target=ingest, args=(2,))
    second = threading.Thread(target=ingest, args=(3,))
    first.start()
    assert held.wait(10)
    second.start()
    second.join(2)  # it must wait for the first, not race it
    release.set()
    first.join(10)
    second.join(10)
    assert not errors

    with factory() as s:
        breaches = s.execute(select(Event).where(Event.type == "zone_breach")).scalars().all()
    assert len(breaches) == 1


def test_one_asset_waiting_does_not_hold_up_another(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pipeline.process_position
    held = threading.Event()
    release = threading.Event()

    def slow(session: Session, pos: AssetPositionV1, created: bool) -> list[EventV1]:
        events = real(session, pos, created)
        if pos.asset_id == "LV-07":
            held.set()
            release.wait(10)
        return events

    monkeypatch.setattr(pipeline, "process_position", slow)

    def ingest(fix: PositionIngest) -> None:
        with factory() as s:
            store_and_process(s, fix)

    stuck = threading.Thread(target=ingest, args=(_fix(0, _MAG),))
    stuck.start()
    assert held.wait(10)
    other = threading.Thread(target=ingest, args=(_fix(0, _FAR, "HT-102"),))
    other.start()
    other.join(5)
    try:
        assert not other.is_alive()  # HT-102 went through while LV-07 was held
    finally:
        release.set()
        stuck.join(10)
