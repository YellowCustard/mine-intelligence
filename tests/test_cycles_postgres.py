"""Postgres integration test for recompute: cycles/metrics land and are idempotent.

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL. Exercises the
recompute SQL (delete/insert, JSONB, group-by) against the real dialect; the
cycle maths itself is covered by the unit tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.cycles.recompute import recompute
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.storage.models import Asset, Base, HaulCycle, Position, Site
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(_DB, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    s.commit()
    s.add(Asset(asset_id="HT-101", site_id="kn-zw-01", asset_class="haul_truck"))
    upsert_zone(
        s,
        site_id="kn-zw-01",
        zone_id="pit-face",
        name="Face",
        kind="loading",
        geometry=box_polygon(FACE[0], FACE[1], 70),
        rules={},
    )
    upsert_zone(
        s,
        site_id="kn-zw-01",
        zone_id="rom-pad",
        name="ROM",
        kind="unloading",
        geometry=box_polygon(ROM[0], ROM[1], 70),
        rules={},
    )
    s.commit()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _store_shift(session: Session) -> None:
    sim = Simulator(seed=1, start=datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC))
    for _ in range(2500):
        for p in sim.step():
            if p.asset_id != "HT-101":
                continue
            session.add(
                Position(
                    site_id=p.site_id,
                    asset_id=p.asset_id,
                    ts=p.ts,
                    received_at=p.ts,
                    lat=p.lat,
                    lon=p.lon,
                    speed_kph=p.speed_kph,
                    ignition=p.ignition,
                    source=p.source,
                )
            )
    session.commit()


def test_recompute_lands_cycles_and_is_idempotent(session: Session) -> None:
    _store_shift(session)
    first = recompute(session, "kn-zw-01")
    assert first["cycles"] >= 3
    assert first["buckets"] >= 1

    n1 = session.execute(select(func.count()).select_from(HaulCycle)).scalar_one()
    # Re-running must not duplicate rows (idempotent replace).
    second = recompute(session, "kn-zw-01")
    n2 = session.execute(select(func.count()).select_from(HaulCycle)).scalar_one()
    assert first == second
    assert n1 == n2

    # Every stored cycle has a positive cycle time and non-negative queue.
    for c in session.execute(select(HaulCycle)).scalars().all():
        assert c.cycle_time_s > 0
        assert c.queue_s >= 0
