"""Loader and truck pairing: a truck loads where it stands beside a tracked loader.

Without the loader's position, loading is taken to be the truck's last long stop in the
load zone. A truck that stops again after loading (waiting at the zone exit, giving way)
for longer than ``MIN_LOAD_S`` then has that stop counted as its loading, and its real
loading counted as queue. With the loader tracked, the stop beside it is the loading.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.config import get_settings
from minemonitor.contracts import AssetPositionV1
from minemonitor.cycles import compute_cycles
from minemonitor.cycles.recompute import recompute, refresh
from minemonitor.cycles.statemachine import Cycle, LoaderFix
from minemonitor.ingest.adapters.simulator import FACE, ROM, Simulator
from minemonitor.ingest.geo import offset_m
from minemonitor.ingest.service import to_canonical
from minemonitor.storage.models import Asset, Base, HaulCycle, Site
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone
from tests.test_recompute_window import _seed

_BASE = (-17.8252, 31.0335)
_LOAD = box_polygon(*_BASE, 70)  # loading spot at 0 m; the line forms south of it
_DUMP = box_polygon(*offset_m(*_BASE, 400, 0), 50)
_LOADS = [("pit-face", _LOAD)]
_DUMPS = [("rom-pad", _DUMP)]
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Fix:
    ts: datetime
    lat: float
    lon: float
    speed_kph: float | None


class _Track:
    """A 1 Hz truck track along one north-south line through the loading spot."""

    def __init__(self, north_m: float) -> None:
        self.t = T0
        self.north_m = north_m
        self.fixes: list[_Fix] = []

    def _emit(self, speed_kph: float) -> None:
        lat, lon = offset_m(*_BASE, self.north_m, 0)
        self.fixes.append(_Fix(self.t, lat, lon, speed_kph))
        self.t += timedelta(seconds=1)

    def stop(self, seconds: int) -> _Track:
        for _ in range(seconds):
            self._emit(0.0)
        return self

    def drive(self, to_north_m: float, kph: float) -> _Track:
        step = kph / 3.6
        while abs(to_north_m - self.north_m) > 1e-9:
            d = to_north_m - self.north_m
            self.north_m = to_north_m if abs(d) <= step else self.north_m + step * (d / abs(d))
            self._emit(kph)
        return self

    def to_dump_and_back(self) -> _Track:
        """Leave the load zone, dump, and come back in (which closes the cycle)."""
        return self.drive(400, 30).stop(40).drive(150, 30).drive(0, 30).stop(60).drive(150, 30)


def _loader(
    asset_id: str, east_m: float, *, every_s: int = 1, jitter_m: float = 2.0, north_m: float = 0.0
) -> list[LoaderFix]:
    """A loader working on the spot ``east_m`` east of the loading spot all session."""
    rng = random.Random(asset_id)
    out = []
    for k in range(0, 3600, every_s):
        lat, lon = offset_m(
            *_BASE, north_m + rng.gauss(0, jitter_m), east_m + rng.gauss(0, jitter_m)
        )
        out.append(LoaderFix(asset_id, T0 + timedelta(seconds=k), lat, lon))
    return out


def _one(track: _Track, loaders: list[LoaderFix] | None = None) -> Cycle:
    cycles = compute_cycles(
        "HT-1", track.fixes, load_zones=_LOADS, dump_zones=_DUMPS, loaders=loaders or []
    )
    assert len(cycles) == 1
    return cycles[0]


def _exit_wait_track(wait_s: int = 45) -> tuple[_Track, float]:
    """Queue 60 s, spot, load 90 s, pull forward and wait ``wait_s`` inside the zone."""
    tr = _Track(150).drive(-15, 30)
    queue_start = tr.t
    tr.stop(60)
    called_forward = tr.t
    tr.drive(0, 8).stop(90).drive(40, 8).stop(wait_s).to_dump_and_back()
    return tr, (called_forward - queue_start).total_seconds()


# --- the state machine ---------------------------------------------------------------


def test_a_long_wait_after_loading_is_taken_for_the_loading_without_the_loader() -> None:
    # The defect pairing fixes: the 45 s exit wait reads as the loading, so the spot and
    # the 90 s of real loading are counted as queue.
    tr, truth = _exit_wait_track()
    c = _one(tr)
    assert c.queue_s > truth + 90
    assert c.loader_id is None


def test_the_stop_beside_the_loader_is_the_loading() -> None:
    tr, truth = _exit_wait_track()
    c = _one(tr, _loader("EX-1", east_m=7))
    assert c.queue_s == truth
    assert c.loader_id == "EX-1"
    assert abs(c.queue_s + c.load_s + c.haul_s + c.dump_s + c.return_s - c.cycle_time_s) < 1e-6


def test_a_loader_reporting_every_ten_seconds_still_pairs() -> None:
    tr, truth = _exit_wait_track()
    c = _one(tr, _loader("EX-1", east_m=7, every_s=10))
    assert (c.queue_s, c.loader_id) == (truth, "EX-1")


def test_a_loader_too_far_away_leaves_the_old_rule() -> None:
    tr, _ = _exit_wait_track()
    without = _one(tr)
    c = _one(tr, _loader("EX-1", east_m=40))
    assert (c.queue_s, c.load_s, c.loader_id) == (without.queue_s, without.load_s, None)


def test_a_loader_with_no_fix_during_the_stop_leaves_the_old_rule() -> None:
    tr, _ = _exit_wait_track()
    without = _one(tr)
    offline = [f for f in _loader("EX-1", east_m=7) if f.ts < T0 + timedelta(seconds=10)]
    c = _one(tr, offline)
    assert (c.queue_s, c.loader_id) == (without.queue_s, None)


def test_the_nearest_of_two_loaders_is_the_one_paired() -> None:
    tr, truth = _exit_wait_track()
    c = _one(tr, _loader("WL-2", east_m=10) + _loader("EX-9", east_m=-6, north_m=1))
    assert (c.queue_s, c.loader_id) == (truth, "EX-9")


def test_waiting_in_line_beside_a_loader_is_not_loading_until_long_enough() -> None:
    # A short pause beside the loader on the way past (shorter than MIN_LOAD_S) is not
    # where the truck loaded: the long stop beside it is.
    tr = _Track(150).drive(-15, 30)
    queue_start = tr.t
    tr.stop(60).drive(-2, 8).stop(10)
    called_forward = tr.t
    tr.drive(6, 4).stop(90).to_dump_and_back()
    c = _one(tr, _loader("EX-1", east_m=7, north_m=4))
    assert (c.queue_s, c.loader_id) == ((called_forward - queue_start).total_seconds(), "EX-1")


def test_a_truck_that_goes_straight_to_the_loader_has_no_queue_and_its_loader() -> None:
    tr = _Track(150).drive(0, 20).stop(90).drive(40, 8).stop(45).to_dump_and_back()
    c = _one(tr, _loader("EX-1", east_m=7))
    assert (c.queue_s, c.loader_id) == (0.0, "EX-1")


def test_a_machine_is_never_its_own_loader() -> None:
    tr, _ = _exit_wait_track()
    own = [LoaderFix("HT-1", f.ts, f.lat, f.lon) for f in tr.fixes]
    assert _one(tr, own).loader_id is None


# --- against the simulator -----------------------------------------------------------

_SIM_LOADS = [("pit-face", box_polygon(FACE[0], FACE[1], 70))]
_SIM_DUMPS = [("rom-pad", box_polygon(ROM[0], ROM[1], 70))]


def _sim(exit_wait_s: float, ticks: int = 4000) -> tuple[Simulator, dict[str, list]]:
    sim = Simulator(seed=1, exit_wait_s=exit_wait_s)
    fixes: dict[str, list] = {}
    for _ in range(ticks):
        for p in sim.step():
            fixes.setdefault(p.asset_id, []).append(p)
    return sim, fixes


def _queue_errors(sim: Simulator, fixes: dict[str, list], loaders: list[LoaderFix]) -> list[float]:
    errors = []
    for aid, fx in fixes.items():
        if not aid.startswith("HT-"):
            continue
        cycles = compute_cycles(
            aid, fx, load_zones=_SIM_LOADS, dump_zones=_SIM_DUMPS, loaders=loaders
        )
        for c in cycles:
            truth = next(
                q for (a, s, _, q) in sim.queue_episodes if a == aid and c.start_ts <= s < c.end_ts
            )
            errors.append(abs(c.queue_s - truth))
            if loaders:
                assert c.loader_id == "EX-01"
    return errors


def test_simulated_exit_waits_need_the_loader_to_measure_queue() -> None:
    sim, fixes = _sim(exit_wait_s=45)
    loaders = [LoaderFix("EX-01", p.ts, p.lat, p.lon) for p in fixes["EX-01"]]
    paired = _queue_errors(sim, fixes, loaders)
    assert len(paired) >= 15
    assert max(paired) <= 1.0  # within one 1 Hz tick of the injected truth, every cycle
    unpaired = _queue_errors(sim, fixes, [])
    assert max(unpaired) > 60  # the old rule counts the loading as queue


# --- stored cycles ---------------------------------------------------------------------

SITE = "kn-zw-01"
KEEP = {"HT-101", "HT-102", "EX-01"}


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _session() -> Session:
    engine = create_engine(
        "sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
    s.commit()  # no relationship() between them: flush order would not put the site first
    s.add(Asset(site_id=SITE, asset_id="EX-01", asset_class="excavator"))
    s.add(Asset(site_id=SITE, asset_id="HT-101", asset_class="haul_truck"))
    s.add(Asset(site_id=SITE, asset_id="HT-102", asset_class="haul_truck"))
    s.commit()
    for zone_id, kind, geom in (
        ("pit-face", "loading", _SIM_LOADS[0][1]),
        ("rom-pad", "unloading", _SIM_DUMPS[0][1]),
    ):
        upsert_zone(
            s, site_id=SITE, zone_id=zone_id, name=zone_id, kind=kind, geometry=geom, rules={}
        )
    s.commit()
    return s


def _shift(steps: int = 3600) -> tuple[Simulator, list[AssetPositionV1]]:
    sim = Simulator(seed=3, start=T0, exit_wait_s=45)
    out: list[AssetPositionV1] = []
    for _ in range(steps):
        out.extend(to_canonical(p) for p in sim.step() if p.asset_id in KEEP)
    return sim, out


def _stored(s: Session) -> list[tuple]:
    s.expire_all()
    return [
        (
            c.asset_id,
            c.start_ts.replace(tzinfo=UTC),  # SQLite hands back naive UTC
            c.end_ts.replace(tzinfo=UTC),
            round(c.queue_s, 6),
            round(c.load_s, 6),
            c.loader_id,
        )
        for c in s.execute(select(HaulCycle).order_by(HaulCycle.asset_id, HaulCycle.start_ts))
        .scalars()
        .all()
    ]


def _assert_true_queues(sim: Simulator, stored: list[tuple]) -> None:
    assert len(stored) >= 4
    for aid, start, end, queue_s, _, loader_id in stored:
        truth = next(q for (a, s, _, q) in sim.queue_episodes if a == aid and start <= s < end)
        assert abs(queue_s - truth) <= 1.0
        assert loader_id == "EX-01"


def test_the_tick_pairs_and_matches_a_full_rebuild() -> None:
    s = _session()
    sim, fixes = _shift()
    for i in range(0, len(fixes), 3 * 97):
        _seed(s, fixes[i : i + 3 * 97])
        refresh(s, SITE)
    incremental = _stored(s)
    recompute(s, SITE)
    assert incremental == _stored(s)
    _assert_true_queues(sim, incremental)


def test_loader_fixes_arriving_late_correct_cycles_already_stored() -> None:
    # The loader's tracker was out of coverage: the trucks' cycles are stored without it
    # (the old rule), then its buffered fixes arrive. The next ticks re-pair them.
    s = _session()
    sim, fixes = _shift()
    trucks = [p for p in fixes if p.asset_id != "EX-01"]
    for i in range(0, len(trucks), 2 * 120):
        _seed(s, trucks[i : i + 2 * 120])
        refresh(s, SITE)
    before = _stored(s)
    assert before and all(row[5] is None for row in before)

    loader = [p for p in fixes if p.asset_id == "EX-01"]
    _seed(s, loader[: len(loader) // 2])  # first, part of the backlog
    refresh(s, SITE)
    _seed(s, list(reversed(loader[len(loader) // 2 :])))  # then the rest, newest first
    refresh(s, SITE)

    after = _stored(s)
    recompute(s, SITE)
    assert after == _stored(s)
    _assert_true_queues(sim, after)


def test_a_gap_in_the_loaders_track_filled_late_re_pairs_the_cycles_in_it() -> None:
    # The loader's fixes for 20 minutes mid-shift arrive after everything else: behind
    # its cursor, so ingest marks the loader, and the trucks' cycles stored across the
    # gap (unpaired) are re-paired from it.
    s = _session()
    sim, fixes = _shift()
    gap = (T0 + timedelta(minutes=10), T0 + timedelta(minutes=30))
    held = [p for p in fixes if p.asset_id == "EX-01" and gap[0] <= p.ts < gap[1]]
    late = {id(p) for p in held}
    _seed(s, [p for p in fixes if id(p) not in late])
    refresh(s, SITE)
    assert any(row[5] is None for row in _stored(s))  # the cycles in the gap
    _seed(s, held)
    refresh(s, SITE)
    after = _stored(s)
    recompute(s, SITE)
    assert after == _stored(s)
    _assert_true_queues(sim, after)


def test_cycles_record_no_loader_when_no_asset_is_a_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_LOADER_CLASSES", "wheel_loader")
    get_settings.cache_clear()
    s = _session()
    _, fixes = _shift()
    _seed(s, fixes)
    recompute(s, SITE)
    stored = _stored(s)
    assert stored and all(row[5] is None for row in stored)
