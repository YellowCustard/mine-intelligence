"""Incremental (windowed) cycle/metric recompute — audit H3/H4.

The maintenance tick used to rebuild a site's whole history every minute (H3) and, by
deleting every derived row it could not rebuild, cut cycle/metric history down to the
raw-position retention window (H4). It now rebuilds each asset from a cursor.

The property that matters: however positions arrive (in order, in chunks, late, out of
order, duplicated) and however often the tick runs, the stored cycles and buckets are
exactly what one whole-history rebuild over the same positions produces.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.config import get_settings
from minemonitor.contracts import AssetPositionV1
from minemonitor.cycles import recompute as rc
from minemonitor.cycles.recompute import recompute, refresh
from minemonitor.cycles.statemachine import MIN_EXIT_S
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.ingest.geo import offset_m
from minemonitor.ingest.service import to_canonical
from minemonitor.storage.models import (
    AssetMetrics,
    Base,
    HaulCycle,
    Position,
    RecomputeCursor,
    Site,
)
from minemonitor.storage.repositories import insert_position
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

SITE = "kn-zw-01"
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)
KEEP = {"HT-101", "HT-102", "EX-01", "LV-07"}  # two trucks still queue for the face
_POSITION_COLUMNS = {
    "site_id",
    "asset_id",
    "ts",
    "received_at",
    "lat",
    "lon",
    "altitude_m",
    "speed_kph",
    "heading_deg",
    "hdop",
    "satellites",
    "ignition",
    "source",
}


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
    s.commit()
    upsert_zone(
        s,
        site_id=SITE,
        zone_id="pit-face",
        name="F",
        kind="loading",
        geometry=box_polygon(FACE[0], FACE[1], 70),
        rules={},
    )
    upsert_zone(
        s,
        site_id=SITE,
        zone_id="rom-pad",
        name="R",
        kind="unloading",
        geometry=box_polygon(ROM[0], ROM[1], 70),
        rules={},
    )
    s.commit()
    return s


def _shift(steps: int, seed: int = 3) -> list[AssetPositionV1]:
    sim = Simulator(seed=seed, start=T0)
    out: list[AssetPositionV1] = []
    for _ in range(steps):
        out.extend(to_canonical(p) for p in sim.step() if p.asset_id in KEEP)
    return out


def _store(s: Session, fixes: list[AssetPositionV1]) -> None:
    for p in fixes:
        insert_position(s, p, commit=False)
    s.commit()


def _seed(s: Session, fixes: list[AssetPositionV1]) -> None:
    """``_store`` for fixes none of which is stored yet, in bulk (setup, not under test).

    Each asset's earliest fix goes through ingest, which marks it exactly as ingesting
    every fix would; the rest are bulk-inserted.
    """
    first: dict[str, AssetPositionV1] = {}
    for p in fixes:
        if p.asset_id not in first or p.ts < first[p.asset_id].ts:
            first[p.asset_id] = p
    rest = [p for p in fixes if first[p.asset_id] is not p]
    if rest:
        s.execute(
            sqlite_insert(Position),
            [p.model_dump(include=_POSITION_COLUMNS) for p in rest],
        )
    _store(s, list(first.values()))


def _derived(s: Session) -> tuple[list[tuple], list[tuple]]:
    s.expire_all()
    cycles = [
        (
            c.asset_id,
            c.start_ts,
            c.end_ts,
            round(c.cycle_time_s, 6),
            round(c.queue_s, 6),
            round(c.load_s, 6),
            round(c.haul_s, 6),
            round(c.dump_s, 6),
            round(c.return_s, 6),
        )
        for c in s.execute(select(HaulCycle).order_by(HaulCycle.asset_id, HaulCycle.start_ts))
        .scalars()
        .all()
    ]
    metrics = [
        (
            m.asset_id,
            m.bucket_start,
            round(m.distance_m, 6),
            round(m.moving_time_s, 6),
            round(m.idle_time_s, 6),
            round(m.max_speed_kph, 6),
            round(m.mean_speed_kph, 6),
            tuple(sorted((k, round(v, 6)) for k, v in m.zone_dwell_s.items())),
            m.loads_completed,
        )
        for m in s.execute(
            select(AssetMetrics).order_by(AssetMetrics.asset_id, AssetMetrics.bucket_start)
        )
        .scalars()
        .all()
    ]
    return cycles, metrics


def _assert_matches_full_rebuild(s: Session) -> tuple[list[tuple], list[tuple]]:
    incremental = _derived(s)
    recompute(s, SITE)  # from each asset's first fix: the whole-history answer
    full = _derived(s)
    assert incremental[0] == full[0]
    assert incremental[1] == full[1]
    return full


def _chunks(fixes: list[AssetPositionV1], seconds: int) -> list[list[AssetPositionV1]]:
    out: dict[int, list[AssetPositionV1]] = {}
    for p in fixes:
        out.setdefault(int((p.ts - T0).total_seconds()) // seconds, []).append(p)
    return [out[k] for k in sorted(out)]


# --- the property --------------------------------------------------------------------


@pytest.mark.parametrize("chunk_s", [60, 137])
def test_ticks_over_in_order_arrival_match_a_full_rebuild(chunk_s: int) -> None:
    s = _session()
    for chunk in _chunks(_shift(3600), chunk_s):
        _store(s, chunk)
        refresh(s, SITE)
    cycles, metrics = _assert_matches_full_rebuild(s)
    assert {"HT-101", "HT-102"} <= {c[0] for c in cycles} and len(cycles) >= 8
    assert sum(m[-1] for m in metrics) == len(cycles)  # every cycle counted as one load


@pytest.mark.parametrize("seed", [1, 2])
def test_late_out_of_order_and_duplicate_arrival_match_a_full_rebuild(seed: int) -> None:
    rng = random.Random(seed)
    fixes = _shift(3600, seed=seed)
    # A truck's tracker loses its link for 20 minutes and backfills after it returns; a
    # second asset's fixes arrive shuffled; some fixes are replayed.
    outage = [
        p
        for p in fixes
        if p.asset_id == "HT-102"
        and T0 + timedelta(minutes=15) <= p.ts < T0 + timedelta(minutes=35)
    ]
    held = {(p.asset_id, p.ts) for p in outage}
    live = [p for p in fixes if (p.asset_id, p.ts) not in held]
    chunks = _chunks(live, 60)
    backfill_at = {45: outage[: len(outage) // 2][::-1], 47: outage[len(outage) // 2 :]}

    s = _session()
    for i, chunk in enumerate(chunks):
        if i % 7 == 3:
            rng.shuffle(chunk)
        if i % 11 == 5:
            chunk = chunk + rng.sample(chunk, k=min(20, len(chunk)))
        _store(s, chunk)
        if i in backfill_at:
            _store(s, backfill_at[i])
        refresh(s, SITE)
    _assert_matches_full_rebuild(s)


def test_backfill_arriving_between_ticks_in_reverse_matches_a_full_rebuild() -> None:
    fixes = _shift(3600)
    s = _session()
    _seed(s, [p for p in fixes if p.asset_id != "HT-101"])
    refresh(s, SITE)
    # The whole of one truck's shift arrives late, newest first, a few minutes per tick.
    truck = sorted((p for p in fixes if p.asset_id == "HT-101"), key=lambda p: p.ts, reverse=True)
    for i in range(0, len(truck), 400):
        _store(s, truck[i : i + 400])
        refresh(s, SITE)
    _assert_matches_full_rebuild(s)


def test_short_cycle_cap_matches_a_full_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulated cycles straddle a 10-minute cap: some are dropped, some kept.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    s = _session()
    for chunk in _chunks(_shift(3600), 90):
        _store(s, chunk)
        refresh(s, SITE)
    cycles, _ = _assert_matches_full_rebuild(s)
    assert cycles and all(c[3] <= 600 for c in cycles)


# --- synthetic tracks ----------------------------------------------------------------

_OUT = offset_m(FACE[0], FACE[1], 0, 300)  # on the haul road, outside both zones


def _fix(asset: str, t: datetime, at: tuple[float, float], kph: float) -> AssetPositionV1:
    return AssetPositionV1(
        site_id=SITE,
        asset_id=asset,
        ts=t,
        received_at=t,
        lat=at[0],
        lon=at[1],
        speed_kph=kph,
        source="test",
    )


Leg = tuple[tuple[float, float], float, int]


def _track(asset: str, start: datetime, legs: list[Leg]) -> tuple[list[AssetPositionV1], datetime]:
    """Fixes at 1 Hz: each leg is (where, speed_kph, seconds). Returns fixes and end time."""
    t, out = start, []
    for at, kph, secs in legs:
        for _ in range(secs):
            out.append(_fix(asset, t, at, kph))
            t += timedelta(seconds=1)
    return out, t


def _cycle_legs(queue_s: int = 60) -> list[Leg]:
    return [
        (FACE, 0.0, queue_s),  # queue in the load zone
        (FACE, 8.0, 5),  # spot forward
        (FACE, 0.0, 90),  # loading
        (_OUT, 30.0, 300),  # haul
        (ROM, 0.0, 40),  # dump
        (_OUT, 30.0, 300),  # return
    ]


# Each analysis also reads the MIN_EXIT_S of fixes (1 Hz) before its window, and the one
# before those, to see whether a visit under way at its start began earlier (M12).
_LEAD_IN = int(MIN_EXIT_S) + 1


def _spy_cycle_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many fixes each per-asset cycle analysis is handed."""
    seen: list[int] = []
    real = rc.analyse_cycles

    def spy(asset_id, fixes, **kwargs):
        seen.append(len(fixes))
        return real(asset_id, fixes, **kwargs)

    monkeypatch.setattr(rc, "analyse_cycles", spy)
    return seen


def test_parked_truck_does_not_make_the_tick_reread_its_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # H3: a truck dumps, then sits in the workshop for 10 h reporting at 1 Hz. The open
    # cycle can never finish within the cap, so the tick must stop re-reading it.
    s = _session()
    fixes, t = _track("HT-200", T0, _cycle_legs() * 2)
    parked, t = _track("HT-200", t, [(_OUT, 0.0, 10 * 3600)])
    _seed(s, fixes + parked)
    refresh(s, SITE)

    seen = _spy_cycle_reads(monkeypatch)
    more, _ = _track("HT-200", t, [(_OUT, 0.0, 60)])
    _store(s, more)
    refresh(s, SITE)
    assert seen and max(seen) <= 61 + _LEAD_IN  # the new minute, not ten hours
    monkeypatch.undo()
    _assert_matches_full_rebuild(s)


def test_digger_working_the_face_all_shift_is_not_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    # An excavator inside the load zone for 12 h: its visit can never open a cycle that
    # fits the cap, so the tick stops re-reading it once the cap has passed.
    s = _session()
    fixes, t = _track("EX-200", T0, [(_OUT, 20.0, 60), (FACE, 2.0, 12 * 3600)])
    _seed(s, fixes)
    refresh(s, SITE)
    seen = _spy_cycle_reads(monkeypatch)
    more, _ = _track("EX-200", t, [(FACE, 2.0, 60)])
    _store(s, more)
    refresh(s, SITE)
    assert seen and max(seen) <= 61 + _LEAD_IN
    monkeypatch.undo()
    _assert_matches_full_rebuild(s)


def test_digger_inside_its_open_visit_costs_only_its_new_fixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Within the cap the digger's visit could still open a cycle, so its entry stays the
    # cursor; but while it stays in the load zone nothing about cycles can change.
    s = _session()
    fixes, t = _track("EX-201", T0, [(_OUT, 20.0, 60), (FACE, 2.0, 3600)])
    _seed(s, fixes)
    refresh(s, SITE)
    seen = _spy_cycle_reads(monkeypatch)
    more, t = _track("EX-201", t, [(FACE, 2.0, 60)])
    _store(s, more)
    refresh(s, SITE)
    assert seen == []  # buckets rebuilt, cycles untouched
    out, _ = _track("EX-201", t, [(_OUT, 20.0, 30)])
    _store(s, out)
    refresh(s, SITE)
    assert seen == [_LEAD_IN + 3600 + 60 + 30]  # it left: analysed once, from its entry
    monkeypatch.undo()
    _assert_matches_full_rebuild(s)


def test_open_cycle_is_reread_only_from_its_own_start(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _session()
    fixes, t = _track("HT-201", T0, _cycle_legs() * 6 + [(FACE, 0.0, 40), (_OUT, 30.0, 120)])
    _seed(s, fixes)
    refresh(s, SITE)
    seen = _spy_cycle_reads(monkeypatch)
    more, _ = _track("HT-201", t, [(_OUT, 30.0, 60)])
    _store(s, more)
    refresh(s, SITE)
    # From the last load-zone entry (40 s in the zone + 2 min hauled + the new minute),
    # not from the start of the six earlier cycles (over 80 minutes).
    assert seen == [_LEAD_IN + 40 + 120 + 60]
    monkeypatch.undo()
    _assert_matches_full_rebuild(s)


def test_late_fix_inside_a_settled_cycle_reopens_it_exactly() -> None:
    s = _session()
    fixes, _ = _track("HT-202", T0, _cycle_legs(queue_s=120) * 4 + [(FACE, 0.0, 30)])
    # Withhold the queue of the second cycle; it arrives after everything else.
    second = T0 + timedelta(seconds=sum(x[2] for x in _cycle_legs(queue_s=120)))
    held = [p for p in fixes if second <= p.ts < second + timedelta(seconds=120)]
    held_ts = {p.ts for p in held}
    _seed(s, [p for p in fixes if p.ts not in held_ts])
    refresh(s, SITE)
    before, _ = _derived(s)
    _store(s, held)
    assert s.get(RecomputeCursor, (SITE, "HT-202")).dirty_from is not None
    refresh(s, SITE)
    after, _ = _assert_matches_full_rebuild(s)
    assert after != before  # the late queue changed the second cycle


def test_cycle_longer_than_the_cap_is_not_reported() -> None:
    s = _session()
    legs = _cycle_legs()
    overnight = legs[:5] + [(_OUT, 0.0, 5 * 3600), (_OUT, 30.0, 300)] + _cycle_legs()
    fixes, _ = _track("HT-203", T0, overnight + [(FACE, 0.0, 30)])
    _seed(s, fixes)
    recompute(s, SITE)
    cycles, _ = _derived(s)
    # The load→(5 h parked)→load span is not a cycle; the next one is.
    assert [round(c[3]) for c in cycles] == [sum(x[2] for x in _cycle_legs())]


def test_cycle_closing_just_inside_the_cap_survives_a_tick_mid_visit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The next load visit enters 595 s after the opener (cap 600 s), and a tick lands
    # 10 s into it: the cap has passed, but that visit is not yet long enough to count.
    # Once it is, it closes a 595 s cycle, so the tick must keep the opener in view.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    s = _session()
    legs: list[Leg] = [
        (_OUT, 30.0, 60),
        (FACE, 0.0, 10),  # opener: queue, spot, load (30 s in the zone)
        (FACE, 8.0, 5),
        (FACE, 0.0, 15),
        (_OUT, 30.0, 200),  # haul
        (ROM, 0.0, 40),  # dump
        (_OUT, 30.0, 595 - 30 - 200 - 40),  # return
        (FACE, 0.0, 11),  # the closing visit, 10 s old at the tick
    ]
    fixes, t = _track("HT-204", T0, legs)
    _seed(s, fixes)
    refresh(s, SITE)
    more, _ = _track("HT-204", t, [(FACE, 0.0, 60), (_OUT, 30.0, 30)])
    _store(s, more)
    refresh(s, SITE)
    cycles, _ = _assert_matches_full_rebuild(s)
    assert [round(c[3]) for c in cycles] == [595]


# --- M12: a truck at a zone's edge that reads outside for a moment ---------------------

_FACE_EDGE_IN = offset_m(FACE[0], FACE[1], -65, 0)  # 5 m inside the face's southern edge
_FACE_EDGE_OUT = offset_m(FACE[0], FACE[1], -75, 0)  # 5 m outside it
_ROM_EDGE_IN = offset_m(ROM[0], ROM[1], -65, 0)
_ROM_EDGE_OUT = offset_m(ROM[0], ROM[1], -75, 0)


def _blip_s(k: int) -> int:
    return 1 + (5 * k) % (int(MIN_EXIT_S) - 1)  # 1..14 s outside: never a real exit


def _jittery_cycle(k: int) -> list[Leg]:
    """A short haul cycle: queueing at the face edge and tipping at the dump edge each read
    outside for a moment, at points and for lengths that shift from cycle to cycle."""
    a, b = 3 + 7 * (k % 7), _blip_s(k)
    return [
        (_FACE_EDGE_IN, 0.0, a),
        (_FACE_EDGE_OUT, 0.0, b),
        (_FACE_EDGE_IN, 0.0, 50 - a),  # queue: 50 s in the zone, and the blip
        (FACE, 8.0, 5),  # spot
        (FACE, 0.0, 40),  # load
        (_OUT, 30.0, 150),
        (_ROM_EDGE_IN, 0.0, 12),
        (_ROM_EDGE_OUT, 0.0, b),
        (_ROM_EDGE_IN, 0.0, 20),
        (_OUT, 30.0, 150 + (11 * k) % 40),
    ]


def _jittery_shift(n: int = 12) -> list[AssetPositionV1]:
    legs = [leg for k in range(n) for leg in _jittery_cycle(k)]
    fixes, _ = _track("HT-300", T0, [*legs, (_FACE_EDGE_IN, 0.0, 30)])
    return fixes


@pytest.mark.parametrize("chunk_s", [13, 97])
def test_ticks_over_edge_jitter_match_a_full_rebuild(chunk_s: int) -> None:
    s = _session()
    for chunk in _chunks(_jittery_shift(), chunk_s):
        _store(s, chunk)
        refresh(s, SITE)
    cycles, _ = _assert_matches_full_rebuild(s)
    # Every cycle is there, from the moment its truck joined the line, and its queue
    # includes the blip.
    assert [c[4] for c in cycles] == [50.0 + _blip_s(k) for k in range(12)]
    assert [c[7] for c in cycles] == [32.0 + _blip_s(k) for k in range(12)]


def test_edge_jitter_arriving_late_and_out_of_order_matches_a_full_rebuild() -> None:
    rng = random.Random(7)
    fixes = _jittery_shift()
    held = fixes[1200:2400]  # 20 minutes held back by a dropped link
    chunks = _chunks(fixes[:1200] + fixes[2400:], 41)
    s = _session()
    for i, chunk in enumerate(chunks):
        if i % 5 == 2:
            rng.shuffle(chunk)
        _store(s, chunk)
        if i == 70:
            _store(s, held[::-1])
        refresh(s, SITE)
    _assert_matches_full_rebuild(s)


def test_rejoining_a_visit_past_the_cap_opens_no_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    # A truck parks in the face past the cap, then reads outside for 3 s and back. The
    # tick that lands in the blip resumes there with no cycle in progress; the fixes back
    # inside rejoin the parked visit (whose cycle is over the cap), so they must not open
    # one. Only the lead-in before the window shows that.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    legs: list[Leg] = [
        (_OUT, 30.0, 60),
        (_FACE_EDGE_IN, 0.0, 700),  # parked
        (_FACE_EDGE_OUT, 0.0, 3),
        (_FACE_EDGE_IN, 0.0, 40),
        (FACE, 8.0, 5),
        (FACE, 0.0, 40),
        (_OUT, 30.0, 150),
        (ROM, 0.0, 40),
        (_OUT, 30.0, 150),
        *_jittery_cycle(1),
        (_FACE_EDGE_IN, 0.0, 30),
    ]
    fixes, _ = _track("HT-301", T0, legs)
    s = _session()
    tick_at = 60 + 700 + 1  # one fix into the blip
    _store(s, fixes[:tick_at])
    refresh(s, SITE)
    for chunk in _chunks(fixes[tick_at:], 60):
        _store(s, chunk)
        refresh(s, SITE)
    cycles, _ = _assert_matches_full_rebuild(s)
    assert len(cycles) == 1  # the jittery cycle after the parked visit's haul


def test_on_demand_slices_through_edge_jitter_match_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slice boundaries every 10 minutes land in blips and part-way through visits.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    fixes = _jittery_shift()

    def rebuilt(chunk: timedelta, chunk_min: timedelta) -> tuple[list[tuple], list[tuple]]:
        monkeypatch.setattr(rc, "_CHUNK", chunk)
        monkeypatch.setattr(rc, "_CHUNK_MIN", chunk_min)
        s = _session()
        _seed(s, fixes)
        recompute(s, SITE)
        return _derived(s)

    sliced = rebuilt(timedelta(minutes=10), timedelta(minutes=1))
    assert sliced == rebuilt(timedelta(days=1), timedelta(days=1))
    assert len(sliced[0]) == 12


# --- H4: derived history outlives raw positions ----------------------------------------


def test_cycles_and_buckets_older_than_raw_positions_survive_tick_and_recompute() -> None:
    s = _session()
    old = T0 - timedelta(days=200)
    s.add(
        HaulCycle(
            site_id=SITE,
            asset_id="HT-101",
            start_ts=old,
            end_ts=old + timedelta(minutes=20),
            cycle_time_s=1200.0,
            queue_s=100.0,
            load_s=90.0,
            haul_s=400.0,
            dump_s=40.0,
            return_s=570.0,
        )
    )
    s.add(
        AssetMetrics(
            site_id=SITE,
            asset_id="HT-101",
            bucket_start=old,
            bucket_end=old + timedelta(minutes=5),
            zone_dwell_s={},
            loads_completed=1,
        )
    )
    s.commit()  # raw positions for these were deleted by retention long ago
    _seed(s, _shift(1800))
    refresh(s, SITE)
    recompute(s, SITE)
    s.expire_all()
    assert s.execute(select(HaulCycle).where(HaulCycle.start_ts == old)).scalar_one()
    assert s.execute(select(AssetMetrics).where(AssetMetrics.bucket_start == old)).scalar_one()


def test_retention_cut_then_full_recompute_changes_nothing() -> None:
    # Retention deletes raw fixes up to mid-way through a bucket and a cycle. A full
    # recompute afterwards must leave every derived row exactly as it was: the cut
    # bucket and the cycle it began cannot be rebuilt from what is left.
    s = _session()
    _seed(s, [p for p in _shift(3600) if p.asset_id == "HT-101"])
    recompute(s, SITE)
    before = _derived(s)
    first_cycle_start = before[0][0][1]
    cut = first_cycle_start + timedelta(seconds=70)  # mid-queue, mid-bucket
    assert cut.second % 300 != 0
    s.query(Position).filter(Position.ts < cut).delete()
    s.commit()
    recompute(s, SITE)
    assert _derived(s) == before


# --- ingest bookkeeping -----------------------------------------------------------------


def test_ingest_marks_only_fixes_behind_the_cursor() -> None:
    s = _session()
    fixes, _ = _track("HT-204", T0, [(_OUT, 30.0, 120)])
    _store(s, fixes[:60])
    cur = s.get(RecomputeCursor, (SITE, "HT-204"))
    assert cur.dirty_from is not None  # never computed: the asset's earliest fix
    refresh(s, SITE)
    s.expire_all()
    assert s.get(RecomputeCursor, (SITE, "HT-204")).dirty_from is None

    _store(s, fixes[90:])  # ahead of through_ts: the next tick reads it anyway
    s.expire_all()
    assert s.get(RecomputeCursor, (SITE, "HT-204")).dirty_from is None
    refresh(s, SITE)

    _store(s, fixes[60:90][::-1])  # behind through_ts: marked at the earliest
    s.expire_all()
    dirty = s.get(RecomputeCursor, (SITE, "HT-204")).dirty_from
    assert dirty.replace(tzinfo=UTC) == fixes[60].ts

    _store(s, fixes[70:80])  # replayed duplicates are not new fixes
    s.expire_all()
    assert s.get(RecomputeCursor, (SITE, "HT-204")).dirty_from.replace(tzinfo=UTC) == fixes[60].ts


def test_one_failing_asset_does_not_stall_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _session()
    _seed(s, _shift(600))
    real = rc._apply

    def flaky(session, site_id, asset_id, *args, **kwargs):
        if asset_id == "HT-102":
            raise RuntimeError("boom")
        return real(session, site_id, asset_id, *args, **kwargs)

    monkeypatch.setattr(rc, "_apply", flaky)
    out = refresh(s, SITE)
    assert out["failed"] == 1 and out["assets"] == len(KEEP) - 1
    monkeypatch.undo()
    assert refresh(s, SITE)["assets"] == 1  # retried next tick
    _assert_matches_full_rebuild(s)


# --- on-demand rebuilds in slices ---------------------------------------------------


def test_recompute_in_slices_matches_one_pass_and_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 10-minute cap makes the lookback short enough for a one-hour shift to be
    # rebuilt in many slices; boundaries fall mid-cycle, mid-visit and mid-queue.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    s = _session()
    fixes = _shift(3600)
    for chunk in _chunks(fixes, 90):
        _store(s, chunk)
        refresh(s, SITE)
    incremental = _derived(s)

    monkeypatch.setattr(rc, "_CHUNK", timedelta(minutes=12))
    monkeypatch.setattr(rc, "_CHUNK_MIN", timedelta(minutes=5))
    slices = _spy_cycle_reads(monkeypatch)
    recompute(s, SITE)
    assert len(slices) > 3 * len(KEEP)  # really sliced
    assert _derived(s) == incremental
    cursors = {
        c.asset_id: (c.cycles_from, c.entry_known, c.in_open_visit, c.metrics_from, c.through_ts)
        for c in s.execute(select(RecomputeCursor)).scalars()
    }

    monkeypatch.setattr(rc, "_CHUNK", timedelta(days=30))  # one pass per asset
    recompute(s, SITE)
    assert _derived(s) == incremental
    s.expire_all()
    for c in s.execute(select(RecomputeCursor)).scalars():
        # The newest slice leaves the cursor where a one-pass rebuild does.
        assert cursors[c.asset_id] == (
            c.cycles_from,
            c.entry_known,
            c.in_open_visit,
            c.metrics_from,
            c.through_ts,
        )

    more = [p for p in _shift(3900) if p.ts > fixes[-1].ts]
    _store(s, more)
    refresh(s, SITE)
    _assert_matches_full_rebuild(s)


def test_slice_boundary_inside_a_data_gap_keeps_the_visit_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tracker drops out just after a truck enters the face and reports again from
    # inside it minutes later. A slice whose read ends in the gap must still see that
    # load visit as long, or the cycle it closes would vanish from a rebuild.
    monkeypatch.setenv("MM_CYCLE_MAX_S", "600")
    get_settings.cache_clear()
    s = _session()
    short = [  # a 335 s cycle, inside the cap
        (FACE, 0.0, 30),
        (FACE, 8.0, 5),
        (FACE, 0.0, 30),
        (_OUT, 30.0, 120),
        (ROM, 0.0, 30),
        (_OUT, 30.0, 120),
    ]
    head, t = _track("HT-205", T0, short)
    enter, t = _track("HT-205", t, [(FACE, 0.0, 1)])
    t += timedelta(minutes=20)  # tracker offline, truck queueing at the face
    tail, _ = _track("HT-205", t, short * 3)
    _seed(s, head + enter + tail)
    # The first slice ends at 06:06; its read stops 675 s later, inside the gap.
    monkeypatch.setattr(rc, "_CHUNK", timedelta(minutes=6))
    monkeypatch.setattr(rc, "_CHUNK_MIN", timedelta(minutes=5))
    recompute(s, SITE)
    sliced = _derived(s)
    monkeypatch.setattr(rc, "_CHUNK", timedelta(days=30))
    recompute(s, SITE)
    assert _derived(s) == sliced
    assert sliced[0][0][2] == enter[0].ts.replace(tzinfo=None)  # first cycle closes there
