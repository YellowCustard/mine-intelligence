"""The haul-cycle state machine, computed from a position stream.

Driven by load-zone and dump-zone membership:

    AT_FACE -> HAULING_LOADED -> AT_DUMP -> RETURNING_EMPTY -> AT_FACE

A cycle spans one load-zone entry to the next. **Queue time** is the time a truck
spends waiting in the load zone for the loader: from its first stop there until it
spots forward onto the loading spot. A line of trucks moves up in steps, so a truck
may stop several times while it waits, creeping forward a truck length between
stops; all of that is queue (audit M2). Loading is its last stop of at least
``MIN_LOAD_S`` before it leaves the zone, and the move onto it is the spot. Stops
closer together than ``MIN_STEP_M`` are one stop (a speed spike or a shuffle under
the bucket is not a move), so the split uses position alone.

Pure and stateless over its inputs, so it can be re-run over stored positions at
any time (brief §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from minemonitor.ingest.geo import haversine_m
from minemonitor.zones.geometry import point_in_polygon

# Below this speed a fix counts as stationary; at/above it, the truck is moving
# (the ~8 kph spot-forward crawl clears this, ending the queue).
MOVE_THRESHOLD_KPH = 3.0

# Ignore zone visits shorter than this: a real load/dump visit lasts tens of
# seconds to minutes, so anything briefer is a GNSS-jitter blip on the boundary
# (e.g. a departing truck momentarily reading back inside) and must not create a
# phantom cycle.
MIN_VISIT_S = 15.0

# Loading a haul truck takes minutes (several passes of the bucket), so a stop shorter
# than this after the truck is loaded (giving way at the zone exit) is not the loading.
MIN_LOAD_S = 30.0

# A truck that stopped and stopped again closer than this to where it was did not move
# up the line or onto the loader: the movement between was GNSS speed noise or a shuffle.
# A move up the line is at least a truck length (~10 m).
MIN_STEP_M = 5.0


class Fix(Protocol):
    ts: datetime
    lat: float
    lon: float
    speed_kph: float | None


@dataclass(frozen=True)
class Cycle:
    """One complete haul cycle and its segment breakdown (seconds)."""

    asset_id: str
    cycle_index: int
    start_ts: datetime
    end_ts: datetime
    cycle_time_s: float
    queue_s: float
    load_s: float
    haul_s: float
    dump_s: float
    return_s: float

    @property
    def queue_pct(self) -> float:
        return 100.0 * self.queue_s / self.cycle_time_s if self.cycle_time_s else 0.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass
class _Visit:
    enter_ts: datetime
    exit_ts: datetime
    start: int
    end: int  # exclusive


def _visits(flags: list[bool], times: list[datetime]) -> list[_Visit]:
    """Contiguous runs where ``flags`` is True, as [enter_ts, exit_ts) spans."""
    out: list[_Visit] = []
    i, n = 0, len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        while i < n and flags[i]:
            i += 1
        # exit_ts is the first fix outside the zone, or the last fix if it ends inside.
        exit_ts = times[i] if i < n else times[i - 1]
        out.append(_Visit(enter_ts=times[start], exit_ts=exit_ts, start=start, end=i))
    return out


@dataclass
class _Stop:
    """A stationary spell within a visit: fixes [start, end), ``end`` the next fix."""

    start: int
    end: int
    n: int  # stationary fixes in it
    lat: float  # their centroid
    lon: float


def _stops(fixes: list[Fix], moving: list[bool], visit: _Visit) -> list[_Stop]:
    """The stops within a visit, merging those closer together than ``MIN_STEP_M``."""
    out: list[_Stop] = []
    i = visit.start
    while i < visit.end:
        if moving[i]:
            i += 1
            continue
        start = i
        while i < visit.end and not moving[i]:
            i += 1
        n = i - start
        lat = sum(fixes[k].lat for k in range(start, i)) / n
        lon = sum(fixes[k].lon for k in range(start, i)) / n
        prev = out[-1] if out else None
        if prev is not None and haversine_m(prev.lat, prev.lon, lat, lon) < MIN_STEP_M:
            total = prev.n + n
            out[-1] = _Stop(
                start=prev.start,
                end=i,
                n=total,
                lat=(prev.lat * prev.n + lat * n) / total,
                lon=(prev.lon * prev.n + lon * n) / total,
            )
        else:
            out.append(_Stop(start=start, end=i, n=n, lat=lat, lon=lon))
    return out


def _queue_seconds(
    times: list[datetime], fixes: list[Fix], moving: list[bool], visit: _Visit
) -> float:
    """Time spent waiting in a load-zone visit: first stop until the spot to the loader."""
    stops = _stops(fixes, moving, visit)
    if not stops:
        return 0.0  # never stopped — drove through, no queue
    last = len(times) - 1

    def _length_s(stop: _Stop) -> float:
        return (times[min(stop.end, last)] - times[stop.start]).total_seconds()

    loading = next(
        (k for k in range(len(stops) - 1, -1, -1) if _length_s(stops[k]) >= MIN_LOAD_S),
        len(stops) - 1,
    )
    if loading == 0:
        return 0.0  # its first stop was the loading: no wait
    # Waiting ends when the truck leaves its last place in line for the loading spot.
    return (times[stops[loading - 1].end] - times[stops[0].start]).total_seconds()


def compute_cycles(
    asset_id: str,
    fixes: list[Fix],
    *,
    load_geom: dict,
    dump_geom: dict,
    move_threshold_kph: float = MOVE_THRESHOLD_KPH,
    min_visit_s: float = MIN_VISIT_S,
) -> list[Cycle]:
    """Compute complete haul cycles for one asset from an ordered position stream."""
    fixes = sorted(fixes, key=lambda f: _aware(f.ts))
    if len(fixes) < 2:
        return []

    times = [_aware(f.ts) for f in fixes]
    in_load = [point_in_polygon(f.lat, f.lon, load_geom) for f in fixes]
    in_dump = [point_in_polygon(f.lat, f.lon, dump_geom) for f in fixes]
    moving = [(f.speed_kph or 0.0) >= move_threshold_kph for f in fixes]

    def _long_enough(v: _Visit) -> bool:
        return (v.exit_ts - v.enter_ts).total_seconds() >= min_visit_s

    load_visits = [v for v in _visits(in_load, times) if _long_enough(v)]
    dump_visits = [v for v in _visits(in_dump, times) if _long_enough(v)]

    cycles: list[Cycle] = []
    for idx in range(len(load_visits) - 1):
        lv, lv_next = load_visits[idx], load_visits[idx + 1]
        # The dump visit that happens between this load and the next.
        dv = next(
            (d for d in dump_visits if lv.exit_ts <= d.enter_ts < lv_next.enter_ts),
            None,
        )
        if dv is None:
            continue  # incomplete cycle (no dump in between) — skip

        queue_s = _queue_seconds(times, fixes, moving, lv)
        load_zone_s = (lv.exit_ts - lv.enter_ts).total_seconds()
        cycles.append(
            Cycle(
                asset_id=asset_id,
                cycle_index=len(cycles),
                start_ts=lv.enter_ts,
                end_ts=lv_next.enter_ts,
                cycle_time_s=(lv_next.enter_ts - lv.enter_ts).total_seconds(),
                queue_s=queue_s,
                load_s=max(0.0, load_zone_s - queue_s),
                haul_s=(dv.enter_ts - lv.exit_ts).total_seconds(),
                dump_s=(dv.exit_ts - dv.enter_ts).total_seconds(),
                return_s=(lv_next.enter_ts - dv.exit_ts).total_seconds(),
            )
        )
    return cycles
