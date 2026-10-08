"""The haul-cycle state machine, computed from a position stream.

Driven by load-zone and dump-zone membership:

    AT_FACE -> HAULING_LOADED -> AT_DUMP -> RETURNING_EMPTY -> AT_FACE

A cycle spans one load-zone entry to the next. **Queue time** is the initial
stationary spell inside the load zone — from when the truck first stops until it
spots forward to the loader (its first movement in the zone). That movement is
what separates queuing (unproductive waiting) from loading, using position alone.

A cycle longer than ``max_cycle_s`` is not a haul cycle (a breakdown, or a truck
parked overnight between a dump and the next load) and is not reported. The cap also
bounds how far back an incremental recompute ever has to look (audit H3).

Pure and stateless over its inputs, so it can be re-run over stored positions at
any time (brief §9). ``analyse_cycles`` additionally returns the *resume point*: the
earliest time from which re-running over the same stream (plus any later fixes)
reproduces every cycle not yet final, so a caller can recompute a window instead of
the whole history and get exactly the same cycles.
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


@dataclass(frozen=True)
class Resume:
    """Where to restart cycle computation so that every non-final cycle is rebuilt.

    ``entry_known`` is True when ``ts`` is the first fix of a load-zone visit (the fix
    before it was outside), so a visit starting there is complete; False when ``ts`` is
    an arbitrary fix, so a load visit already in progress there has an unknown start and
    must not open a cycle.
    """

    ts: datetime
    entry_known: bool
    # The stream's last fix is still inside the load visit that starts at ``ts``: until a
    # fix lands outside the load zone, nothing about the cycles can change.
    inside: bool = False


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
    max_cycle_s: float | None = None,
    leading_entry_known: bool = True,
) -> list[Cycle]:
    """Compute complete haul cycles for one asset from an ordered position stream."""
    cycles, _ = analyse_cycles(
        asset_id,
        fixes,
        load_geom=load_geom,
        dump_geom=dump_geom,
        move_threshold_kph=move_threshold_kph,
        min_visit_s=min_visit_s,
        max_cycle_s=max_cycle_s,
        leading_entry_known=leading_entry_known,
    )
    return cycles


def analyse_cycles(
    asset_id: str,
    fixes: list[Fix],
    *,
    load_geom: dict,
    dump_geom: dict,
    move_threshold_kph: float = MOVE_THRESHOLD_KPH,
    min_visit_s: float = MIN_VISIT_S,
    max_cycle_s: float | None = None,
    leading_entry_known: bool = True,
) -> tuple[list[Cycle], Resume | None]:
    """Cycles in ``fixes``, and the point a later recompute must restart from.

    ``leading_entry_known=False`` says the stream starts at an arbitrary fix rather than at
    the true start of the asset's track: a load visit in progress at the first fix began
    earlier, so its queue and cycle start are unknown and it opens no cycle.

    Returns ``(cycles, None)`` for an empty stream.
    """
    fixes = sorted(fixes, key=lambda f: _aware(f.ts))
    if not fixes:
        return [], None

    times = [_aware(f.ts) for f in fixes]
    in_load = [point_in_polygon(f.lat, f.lon, load_geom) for f in fixes]
    in_dump = [point_in_polygon(f.lat, f.lon, dump_geom) for f in fixes]
    moving = [(f.speed_kph or 0.0) >= move_threshold_kph for f in fixes]

    def _long_enough(v: _Visit) -> bool:
        return (v.exit_ts - v.enter_ts).total_seconds() >= min_visit_s

    def _can_open(v: _Visit) -> bool:
        return v.start > 0 or leading_entry_known

    load_runs = _visits(in_load, times)
    load_visits = [v for v in load_runs if _long_enough(v)]
    dump_visits = [v for v in _visits(in_dump, times) if _long_enough(v)]

    cycles: list[Cycle] = []
    for idx in range(len(load_visits) - 1):
        lv, lv_next = load_visits[idx], load_visits[idx + 1]
        if not _can_open(lv):
            continue  # began before the stream did: its start and queue are unknown
        cycle_time_s = (lv_next.enter_ts - lv.enter_ts).total_seconds()
        if max_cycle_s is not None and cycle_time_s > max_cycle_s:
            continue  # not a haul cycle (breakdown, parked between dump and load)
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
                cycle_time_s=cycle_time_s,
                queue_s=queue_s,
                load_s=max(0.0, load_zone_s - queue_s),
                haul_s=(dv.enter_ts - lv.exit_ts).total_seconds(),
                dump_s=(dv.exit_ts - dv.enter_ts).total_seconds(),
                return_s=(lv_next.enter_ts - dv.exit_ts).total_seconds(),
            )
        )

    last = times[-1]
    resume = Resume(ts=last, entry_known=False)
    openers = [v for v in load_visits if _can_open(v)]
    if openers and (
        max_cycle_s is None or (last - openers[-1].enter_ts).total_seconds() < max_cycle_s
    ):
        # The cycle this visit opens is still in progress (its end is the next visit,
        # not yet seen) and can still finish within the cap: rebuild from its entry.
        # Everything before it is final, including the cycle that ends at this entry.
        resume = Resume(ts=openers[-1].enter_ts, entry_known=True)
    elif in_load[-1] and not _long_enough(load_runs[-1]) and _can_open(load_runs[-1]):
        # A load visit has just begun and is not yet long enough to count: once it is,
        # it may open a cycle, so rebuild from its entry.
        resume = Resume(ts=load_runs[-1].enter_ts, entry_known=True)
    # Otherwise no cycle is in progress (none opened, or the open one has run past the
    # cap): nothing before the last fix can change, so restart there.
    if resume.entry_known and in_load[-1] and load_runs[-1].enter_ts == resume.ts:
        resume = Resume(ts=resume.ts, entry_known=True, inside=True)
    return cycles, resume
