"""M4 acceptance: computed queue time matches the simulator's injected truth.

The strongest form: match each computed cycle to the ground-truth queue episode
inside it and require near-exact agreement, plus an aggregate within 5%.
"""

from __future__ import annotations

from minemonitor.cycles import compute_cycles
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.zones.geometry import box_polygon

_LOAD = box_polygon(FACE[0], FACE[1], 70)
_DUMP = box_polygon(ROM[0], ROM[1], 70)


def _run(ticks: int = 4000, seed: int = 1):
    sim = Simulator(seed=seed)
    fixes: dict[str, list] = {}
    for _ in range(ticks):
        for p in sim.step():
            fixes.setdefault(p.asset_id, []).append(p)
    return sim, fixes


def test_cycles_are_detected_for_all_trucks() -> None:
    _, fixes = _run()
    for aid, fx in fixes.items():
        if not aid.startswith("HT-"):
            continue
        cycles = compute_cycles(aid, fx, load_geom=_LOAD, dump_geom=_DUMP)
        assert len(cycles) >= 3  # several complete cycles in the window


def test_segments_sum_to_cycle_time() -> None:
    _, fixes = _run()
    cycles = compute_cycles("HT-101", fixes["HT-101"], load_geom=_LOAD, dump_geom=_DUMP)
    for c in cycles:
        seg = c.queue_s + c.load_s + c.haul_s + c.dump_s + c.return_s
        assert abs(seg - c.cycle_time_s) < 1e-6


def test_per_cycle_queue_matches_injected_truth() -> None:
    """Each cycle's queue equals the ground-truth episode inside it (±1 tick)."""
    sim, fixes = _run()
    matched = 0
    for aid, fx in fixes.items():
        if not aid.startswith("HT-"):
            continue
        cycles = compute_cycles(aid, fx, load_geom=_LOAD, dump_geom=_DUMP)
        episodes = [e for e in sim.queue_episodes if e[0] == aid]
        for c in cycles:
            ep = next((q for (a, s, e, q) in episodes if c.start_ts <= s < c.end_ts), None)
            assert ep is not None, f"no ground-truth queue episode in cycle {c.cycle_index}"
            assert abs(c.queue_s - ep) <= 1.0  # within one 1 Hz tick
            matched += 1
    assert matched >= 15  # plenty of cycles checked across the fleet


def test_aggregate_queue_within_five_percent() -> None:
    sim, fixes = _run()
    computed = truth = 0.0
    for aid, fx in fixes.items():
        if not aid.startswith("HT-"):
            continue
        cycles = compute_cycles(aid, fx, load_geom=_LOAD, dump_geom=_DUMP)
        if not cycles:
            continue
        computed += sum(c.queue_s for c in cycles)
        for c in cycles:
            ep = next(
                (
                    q
                    for (a, s, e, q) in sim.queue_episodes
                    if a == aid and c.start_ts <= s < c.end_ts
                ),
                0.0,
            )
            truth += ep
    assert truth > 0
    assert abs(computed - truth) / truth <= 0.05


def test_drive_through_load_zone_has_no_queue() -> None:
    """A truck that never stops in the load zone records zero queue."""
    _, fixes = _run()
    # Patrol vehicles pass through zones without queuing.
    cycles = compute_cycles("WB-01", fixes["WB-01"], load_geom=_LOAD, dump_geom=_DUMP)
    for c in cycles:
        assert c.queue_s == 0.0
