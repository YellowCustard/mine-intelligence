"""Postgres: the incremental recompute and ingest cannot lose a fix between them (H3).

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL. SQLite serialises writers,
so only a real server shows the race this guards: a fix stored while the maintenance
tick is rebuilding its asset, behind the newest fix the tick read, must still be marked
for the next tick instead of falling between the two.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.config import get_settings
from minemonitor.contracts import AssetPositionV1
from minemonitor.cycles import recompute as rc
from minemonitor.cycles.recompute import recompute, refresh
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.ingest.service import to_canonical
from minemonitor.storage.models import AssetMetrics, Base, HaulCycle, RecomputeCursor, Site
from minemonitor.storage.repositories import insert_position
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)

SITE = "kn-zw-01"
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_DB, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
        s.commit()
        for zone_id, kind, at in (("pit-face", "loading", FACE), ("rom-pad", "unloading", ROM)):
            upsert_zone(
                s,
                site_id=SITE,
                zone_id=zone_id,
                name=zone_id,
                kind=kind,
                geometry=box_polygon(at[0], at[1], 70),
                rules={},
            )
        s.commit()
    try:
        yield make
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _truck(steps: int) -> list[AssetPositionV1]:
    sim = Simulator(seed=1, start=T0)
    return [to_canonical(p) for _ in range(steps) for p in sim.step() if p.asset_id == "HT-101"]


def _store(s: Session, fixes: list[AssetPositionV1]) -> None:
    for p in fixes:
        insert_position(s, p, commit=False)
    s.commit()


def _derived(s: Session) -> tuple[list[tuple], list[tuple]]:
    s.expire_all()
    cycles = s.execute(
        select(HaulCycle.start_ts, HaulCycle.end_ts, HaulCycle.queue_s).order_by(HaulCycle.start_ts)
    ).all()
    buckets = s.execute(
        select(
            AssetMetrics.bucket_start, AssetMetrics.distance_m, AssetMetrics.loads_completed
        ).order_by(AssetMetrics.bucket_start)
    ).all()
    return [tuple(r) for r in cycles], [tuple(b) for b in buckets]


def test_fix_stored_during_a_rebuild_is_marked_for_the_next_one(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixes = _truck(1800)
    held = fixes[1500]  # will arrive while the tick rebuilds, behind what it read
    with factory() as s:
        _store(s, fixes[:1200])
        refresh(s, SITE)
        _store(s, [p for p in fixes[1200:] if p is not held])

    in_rebuild, release = threading.Event(), threading.Event()
    real = rc.analyse_cycles

    def paused(*args, **kwargs):
        in_rebuild.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(rc, "analyse_cycles", paused)
    tick_errors: list[BaseException] = []

    def tick() -> None:
        try:
            with factory() as s:
                refresh(s, SITE)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            tick_errors.append(exc)

    t_tick = threading.Thread(target=tick)
    t_tick.start()
    assert in_rebuild.wait(10)  # the tick holds HT-101's cursor and has read its fixes

    def ingest() -> None:
        with factory() as s:
            _store(s, [held])

    t_ingest = threading.Thread(target=ingest)
    t_ingest.start()
    t_ingest.join(1.0)
    assert t_ingest.is_alive()  # ingest waits for the rebuild instead of racing it
    release.set()
    t_tick.join(10)
    t_ingest.join(10)
    assert not tick_errors and not t_tick.is_alive() and not t_ingest.is_alive()
    monkeypatch.undo()

    with factory() as s:
        cur = s.get(RecomputeCursor, (SITE, "HT-101"))
        assert cur.through_ts > held.ts  # the tick read past it without seeing it...
        assert cur.dirty_from == held.ts  # ...so ingest marked it for the next run
        refresh(s, SITE)
        incremental = _derived(s)
        recompute(s, SITE)
        assert _derived(s) == incremental


def test_late_backfill_matches_a_full_rebuild_on_postgres(factory: sessionmaker[Session]) -> None:
    fixes = _truck(3600)
    lost_from, lost_until = T0 + timedelta(minutes=20), T0 + timedelta(minutes=35)
    outage = {p.ts for p in fixes if lost_from <= p.ts < lost_until}
    with factory() as s:
        live = [p for p in fixes if p.ts not in outage]
        for i in range(0, len(live), 60):
            _store(s, live[i : i + 60])
            if i == 2400:
                _store(s, [p for p in fixes if p.ts in outage][::-1])
            refresh(s, SITE)
        incremental = _derived(s)
        recompute(s, SITE)
        assert _derived(s) == incremental
        assert len(incremental[0]) >= 3


def test_older_slices_of_an_on_demand_rebuild_do_not_hold_up_ingest(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short cap and slices make a one-hour track rebuild in several slices.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    monkeypatch.setattr(rc, "_CHUNK", timedelta(minutes=10))
    monkeypatch.setattr(rc, "_CHUNK_MIN", timedelta(minutes=5))
    fixes = _truck(3600)
    late = fixes[1500]
    with factory() as s:
        _store(s, [p for p in fixes[:3000] if p is not late])
        refresh(s, SITE)

    in_rebuild, release = threading.Event(), threading.Event()
    real = rc.analyse_cycles
    calls: list[int] = []

    def paused(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:  # the oldest slice
            in_rebuild.set()
            assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(rc, "analyse_cycles", paused)
    errors: list[BaseException] = []

    def admin() -> None:
        try:
            with factory() as s:
                recompute(s, SITE)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    t_admin = threading.Thread(target=admin)
    t_admin.start()
    assert in_rebuild.wait(10)

    def ingest() -> None:
        with factory() as s:
            _store(s, [late, *fixes[3000:3060]])

    t_ingest = threading.Thread(target=ingest)
    t_ingest.start()
    t_ingest.join(5)
    assert not t_ingest.is_alive()  # stored while the rebuild holds its oldest slice
    release.set()
    t_admin.join(30)
    assert not errors and not t_admin.is_alive()
    assert len(calls) > 2
    monkeypatch.undo()
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()

    with factory() as s:
        cur = s.get(RecomputeCursor, (SITE, "HT-101"))
        assert cur.dirty_from == late.ts  # still marked for the tick
        refresh(s, SITE)
        incremental = _derived(s)
        recompute(s, SITE)
        assert _derived(s) == incremental
