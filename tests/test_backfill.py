"""Backfill / late-data correctness (M6, brief §3).

Positions can arrive out of order and duplicated. After a recompute, derived
cycles must be identical to the in-order case — the analytics are recomputable
and order-independent.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.cycles.recompute import recompute
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.ingest.service import to_canonical
from minemonitor.storage.models import Asset, Base, HaulCycle, Site
from minemonitor.storage.repositories import insert_position
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone


def _fresh_session() -> Session:
    engine = create_engine(
        "sqlite://", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    s.commit()
    s.add(Asset(asset_id="HT-101", site_id="kn-zw-01", asset_class="haul_truck"))
    upsert_zone(s, site_id="kn-zw-01", zone_id="pit-face", name="F", kind="loading",
                geometry=box_polygon(FACE[0], FACE[1], 70), rules={})
    upsert_zone(s, site_id="kn-zw-01", zone_id="rom-pad", name="R", kind="unloading",
                geometry=box_polygon(ROM[0], ROM[1], 70), rules={})
    s.commit()
    return s


@pytest.fixture
def shift_fixes() -> list:
    sim = Simulator(seed=1)
    out = []
    for _ in range(2500):
        for p in sim.step():
            if p.asset_id == "HT-101":
                out.append(to_canonical(p))
    return out


def _cycles(session: Session) -> list[tuple]:
    rows = session.execute(select(HaulCycle).order_by(HaulCycle.start_ts)).scalars().all()
    return [(c.start_ts, round(c.queue_s, 3), round(c.cycle_time_s, 3)) for c in rows]


def test_out_of_order_and_duplicate_arrival_matches_in_order(shift_fixes: list) -> None:
    # In-order insertion.
    in_order = _fresh_session()
    for p in shift_fixes:
        insert_position(in_order, p, commit=False)
    in_order.commit()
    recompute(in_order, "kn-zw-01")
    canonical = _cycles(in_order)
    assert len(canonical) >= 3

    # Shuffled arrival, with a batch of duplicates replayed (store-and-forward).
    shuffled = list(shift_fixes)
    random.Random(99).shuffle(shuffled)
    shuffled += shuffled[:200]  # late duplicates

    late = _fresh_session()
    for p in shuffled:
        insert_position(late, p, commit=False)
    late.commit()
    recompute(late, "kn-zw-01")

    assert _cycles(late) == canonical  # order- and duplicate-independent
