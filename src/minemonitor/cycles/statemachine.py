"""The haul-cycle state machine, computed from a position stream.

Driven by load-zone and dump-zone membership:

    AT_FACE -> HAULING_LOADED -> AT_DUMP -> RETURNING_EMPTY -> AT_FACE

A cycle spans one load-zone entry to the next. **Queue time** is the initial
stationary spell inside the load zone — from when the truck first stops until it
spots forward to the loader (its first movement in the zone). That movement is
what separates queuing (unproductive waiting) from loading, using position alone.

Pure and stateless over its inputs, so it can be re-run over stored positions at
any time (brief §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from minemonitor.zones.geometry import point_in_polygon

# Below this speed a fix counts as stationary; at/above it, the truck is moving
# (the ~8 kph spot-forward crawl clears this, ending the queue).
MOVE_THRESHOLD_KPH = 3.0

# Ignore zone visits shorter than this: a real load/dump visit lasts tens of
# seconds to minutes, so anything briefer is a GNSS-jitter blip on the boundary
# (e.g. a departing truck momentarily reading back inside) and must not create a
# phantom cycle.
MIN_VISIT_S = 15.0


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


def _queue_seconds(times: list[datetime], moving: list[bool], visit: _Visit) -> float:
    """Initial stationary spell within a load-zone visit = queue time."""
    s = None
    for i in range(visit.start, visit.end):
        if not moving[i]:
            s = i
            break
    if s is None:
        return 0.0  # never stopped — drove through, no queue
    for j in range(s + 1, visit.end):
        if moving[j]:
            return (times[j] - times[s]).total_seconds()  # first movement = spotting
    # Still stationary at the end of the visit.
    return (times[visit.end - 1] - times[s]).total_seconds()


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

        queue_s = _queue_seconds(times, moving, lv)
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
