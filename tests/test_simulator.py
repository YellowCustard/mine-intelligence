"""Unit tests for the movement simulator (a correctness-critical fixture)."""

from __future__ import annotations

from minemonitor.ingest.adapters.simulator import MAGAZINE, Simulator
from minemonitor.ingest.geo import haversine_m
from minemonitor.ingest.service import to_canonical


def test_fleet_has_nine_assets() -> None:
    sim = Simulator(seed=1)
    assert len(sim.assets) == 9
    classes = {a.asset_class for a in sim.assets.values()}
    assert {"haul_truck", "excavator", "light_vehicle"} <= classes


def test_emits_one_fix_per_asset_per_tick() -> None:
    sim = Simulator(seed=1)
    seen: dict[str, int] = {a: 0 for a in sim.assets}
    for _ in range(120):
        batch = sim.step()
        assert len(batch) == len(sim.assets)
        for p in batch:
            seen[p.asset_id] += 1
    assert set(seen.values()) == {120}


def test_every_fix_is_contract_valid() -> None:
    sim = Simulator(seed=2)
    for _ in range(300):
        for payload in sim.step():
            canonical = to_canonical(payload)  # validates + stamps received_at
            assert canonical.schema_ == "asset.position.v1"
            assert -90 <= canonical.lat <= 90
            assert canonical.speed_kph is None or canonical.speed_kph >= 0


def test_timestamps_advance_at_one_hz() -> None:
    sim = Simulator(seed=1, tick_s=1.0)
    first = sim.step()[0].ts
    for _ in range(9):
        last = sim.step()[0].ts
    assert (last - first).total_seconds() == 9.0


def test_trucks_accumulate_queue_time_at_the_face() -> None:
    """The shared loader forces trucks to queue — the metric M4 measures."""
    sim = Simulator(seed=1)
    for _ in range(3600):  # one simulated hour
        sim.step()
    trucks = [a for a in sim.assets.values() if a.asset_class == "haul_truck"]
    assert all(t.queue_total_s > 0 for t in trucks)


def test_light_vehicle_reaches_the_restricted_magazine() -> None:
    """LV-07 must actually enter the magazine so M3 has a breach to catch."""
    sim = Simulator(seed=1)
    closest = 1e9
    for _ in range(3600):
        for p in sim.step():
            if p.asset_id == "LV-07":
                closest = min(closest, haversine_m(p.lat, p.lon, MAGAZINE[0], MAGAZINE[1]))
    assert closest < 20.0  # comes within 20 m of the magazine centre


def test_deterministic_for_a_given_seed() -> None:
    a = Simulator(seed=7)
    b = Simulator(seed=7)
    for _ in range(200):
        pa, pb = a.step(), b.step()
        assert [(p.asset_id, p.lat, p.lon) for p in pa] == [(p.asset_id, p.lat, p.lon) for p in pb]
