"""The haul-cycle state machine, computed from a position stream.

Driven by load-zone and dump-zone membership:

    AT_FACE -> HAULING_LOADED -> AT_DUMP -> RETURNING_EMPTY -> AT_FACE

A cycle spans one load-zone entry to the next. Every loading zone counts as the load
zone and every unloading zone as the dump (audit M3): a site with a ROM pad and a waste
dump, or two faces, has cycles through each. Fixes in touching zones of one kind make one
visit, credited to the zone with most of its fixes, and each cycle records where it
loaded and where it dumped. A truck standing at a zone's edge that reads outside for less
than ``MIN_EXIT_S`` has not left: the runs either side are one visit (audit M12).

**Queue time** is the time a truck spends waiting in the load zone for the loader: from
its first stop there until it spots forward onto the loading spot. A line of trucks moves
up in steps, so a truck may stop several times while it waits, creeping forward a truck
length between stops; all of that is queue (audit M2). Loading is its last stop of at
least ``MIN_LOAD_S`` before it leaves the zone, and the move onto it is the spot. Stops
closer together than ``MIN_STEP_M`` are one stop (a speed spike or a shuffle under the
bucket is not a move), so the split uses position alone.

Where the loader itself is tracked, its position decides instead (loader and truck
pairing): loading begins at the truck's first stop of at least ``MIN_LOAD_S`` within
``pairing_m`` of a loader, and the cycle records which loader. That also gets right the
truck that stops again after loading (waiting at the zone exit, giving way) for longer
than ``MIN_LOAD_S``, which the rule above takes for the loading, counting the real
loading as queue. With no loader beside any stop (none tracked, its tracker offline, or
it is out of range), the rule above stands. Segment semantics are the same either way:
load time runs from the end of the queue until the truck leaves the zone.

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

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
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

# A truck standing at a zone's edge (queueing at the face, tipping at the dump) reads
# outside it for a fix or two as GNSS jitters. Time outside shorter than this between
# two runs inside is that jitter, not a trip out and back, so the runs are one visit
# (audit M12): nothing leaves a load zone, goes anywhere and returns this quickly.
MIN_EXIT_S = 15.0

# Loading a haul truck takes minutes (several passes of the bucket), so a stop shorter
# than this after the truck is loaded (giving way at the zone exit) is not the loading.
MIN_LOAD_S = 30.0

# A truck that stopped and stopped again closer than this to where it was did not move
# up the line or onto the loader: the movement between was GNSS speed noise or a shuffle.
# A move up the line is at least a truck length (~10 m).
MIN_STEP_M = 5.0

# A truck stopped with a loader's median position within this of its own is beside it,
# being loaded. A truck under an excavator's bucket or at a wheel loader stands a few
# metres from the loader's GNSS antenna; the truck waiting behind it a truck length
# (~15 m) further. Calibrate per site against the real loading geometry.
PAIRING_M = 12.0

# Loader fixes this long either side of a truck's stop count toward its position during
# the stop, so a loader reporting less often than the truck (or a fix or two late) still
# pairs.
LOADER_MATCH_S = 30.0

# A zone as the analysis sees it: its id and its GeoJSON polygon.
ZoneShape = tuple[str, dict]


class Fix(Protocol):
    ts: datetime
    lat: float
    lon: float
    speed_kph: float | None


@dataclass(frozen=True)
class LoaderFix:
    """A position of a loading machine (excavator, wheel loader), for pairing."""

    asset_id: str
    ts: datetime
    lat: float
    lon: float


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
    load_zone_id: str | None = None
    dump_zone_id: str | None = None
    # The loader the truck stood beside while it loaded; None when no tracked loader was.
    loader_id: str | None = None

    @property
    def queue_pct(self) -> float:
        return 100.0 * self.queue_s / self.cycle_time_s if self.cycle_time_s else 0.0


@dataclass(frozen=True)
class Resume:
    """Where to restart cycle computation so that every non-final cycle is rebuilt.

    ``entry_known`` is True when ``ts`` is the first fix of a load-zone visit (the fixes
    before it were outside, for at least ``MIN_EXIT_S``), so a visit starting there is
    complete; False when ``ts`` is an arbitrary fix, so a load visit already in progress
    there has an unknown start and must not open a cycle.
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


def _zone_at(lat: float, lon: float, zones: Sequence[ZoneShape]) -> str | None:
    """The first of ``zones`` containing the point, if any."""
    return next((zid for zid, geom in zones if point_in_polygon(lat, lon, geom)), None)


def _credited_zone(visit: _Visit, labels: list[str | None]) -> str | None:
    """The zone holding most of the visit's fixes; on a tie, the one it was in first."""
    counts = Counter(labels[k] for k in range(visit.start, visit.end) if labels[k] is not None)
    return counts.most_common(1)[0][0] if counts else None


def _visits(flags: list[bool], times: list[datetime], min_exit_s: float) -> list[_Visit]:
    """Runs where ``flags`` is True, as [enter_ts, exit_ts) spans.

    Runs less than ``min_exit_s`` apart are one visit, briefly read outside: its fixes
    span both runs and the ones between.
    """
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
        prev = out[-1] if out else None
        if prev is not None and (times[start] - prev.exit_ts).total_seconds() < min_exit_s:
            prev.exit_ts, prev.end = exit_ts, i
        else:
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


class _Loaders:
    """Loader fixes per loader, in time order, for looking up a stop's neighbours."""

    def __init__(self, fixes: Sequence[LoaderFix], exclude: str) -> None:
        by_id: dict[str, list[LoaderFix]] = {}
        for f in fixes:
            if f.asset_id != exclude:  # a machine is never its own loader
                by_id.setdefault(f.asset_id, []).append(f)
        self._fixes = {
            aid: sorted(fx, key=lambda f: _aware(f.ts)) for aid, fx in sorted(by_id.items())
        }
        self._times = {aid: [_aware(f.ts) for f in fx] for aid, fx in self._fixes.items()}

    def __bool__(self) -> bool:
        return bool(self._fixes)

    def beside(
        self, lat: float, lon: float, since: datetime, until: datetime, pairing_m: float
    ) -> str | None:
        """The loader whose median distance from (lat, lon) over [since, until] is least
        and within ``pairing_m``; on a tie, the lowest asset id."""
        best: tuple[float, str] | None = None
        for aid, fx in self._fixes.items():
            times = self._times[aid]
            lo, hi = bisect_left(times, since), bisect_right(times, until)
            if lo == hi:
                continue  # no fix from this loader then: it cannot say where it was
            d = median(haversine_m(lat, lon, f.lat, f.lon) for f in fx[lo:hi])
            if d <= pairing_m and (best is None or (d, aid) < best):
                best = (d, aid)
        return best[1] if best is not None else None


def _queue_seconds(
    times: list[datetime],
    fixes: list[Fix],
    moving: list[bool],
    visit: _Visit,
    loaders: _Loaders,
    pairing_m: float,
) -> tuple[float, str | None]:
    """Time spent waiting in a load-zone visit (first stop until the spot to the loader),
    and the tracked loader the truck stood beside to load, if any."""
    stops = _stops(fixes, moving, visit)
    if not stops:
        return 0.0, None  # never stopped — drove through, no queue
    last = len(times) - 1
    match = timedelta(seconds=LOADER_MATCH_S)

    def _length_s(stop: _Stop) -> float:
        return (times[min(stop.end, last)] - times[stop.start]).total_seconds()

    loading: int | None = None
    loader_id: str | None = None
    if loaders:
        for k, stop in enumerate(stops):
            if _length_s(stop) < MIN_LOAD_S:
                continue
            # The stop's stationary fixes run to the one before ``end``.
            loader_id = loaders.beside(
                stop.lat,
                stop.lon,
                times[stop.start] - match,
                times[stop.end - 1] + match,
                pairing_m,
            )
            if loader_id is not None:
                loading = k
                break
    if loading is None:
        loading = next(
            (k for k in range(len(stops) - 1, -1, -1) if _length_s(stops[k]) >= MIN_LOAD_S),
            len(stops) - 1,
        )
    if loading == 0:
        return 0.0, loader_id  # its first stop was the loading: no wait
    # Waiting ends when the truck leaves its last place in line for the loading spot.
    return (times[stops[loading - 1].end] - times[stops[0].start]).total_seconds(), loader_id


def compute_cycles(
    asset_id: str,
    fixes: list[Fix],
    *,
    load_zones: Sequence[ZoneShape],
    dump_zones: Sequence[ZoneShape],
    move_threshold_kph: float = MOVE_THRESHOLD_KPH,
    min_visit_s: float = MIN_VISIT_S,
    min_exit_s: float = MIN_EXIT_S,
    max_cycle_s: float | None = None,
    leading_entry_known: bool = True,
    opens_from: datetime | None = None,
    loaders: Sequence[LoaderFix] = (),
    pairing_m: float = PAIRING_M,
) -> list[Cycle]:
    """Compute complete haul cycles for one asset from an ordered position stream."""
    cycles, _ = analyse_cycles(
        asset_id,
        fixes,
        load_zones=load_zones,
        dump_zones=dump_zones,
        move_threshold_kph=move_threshold_kph,
        min_visit_s=min_visit_s,
        min_exit_s=min_exit_s,
        max_cycle_s=max_cycle_s,
        leading_entry_known=leading_entry_known,
        opens_from=opens_from,
        loaders=loaders,
        pairing_m=pairing_m,
    )
    return cycles


def analyse_cycles(
    asset_id: str,
    fixes: list[Fix],
    *,
    load_zones: Sequence[ZoneShape],
    dump_zones: Sequence[ZoneShape],
    move_threshold_kph: float = MOVE_THRESHOLD_KPH,
    min_visit_s: float = MIN_VISIT_S,
    min_exit_s: float = MIN_EXIT_S,
    max_cycle_s: float | None = None,
    leading_entry_known: bool = True,
    opens_from: datetime | None = None,
    loaders: Sequence[LoaderFix] = (),
    pairing_m: float = PAIRING_M,
) -> tuple[list[Cycle], Resume | None]:
    """Cycles in ``fixes``, and the point a later recompute must restart from.

    ``leading_entry_known=False`` says the stream starts at an arbitrary fix rather than at
    the true start of the asset's track: a load visit in progress at the first fix began
    earlier, so its queue and cycle start are unknown and it opens no cycle.

    ``opens_from`` marks the fixes before it as lead-in: they are read only to tell
    whether a visit after it began earlier (a run inside less than ``min_exit_s`` before
    rejoins it), and a visit that began before it opens no cycle. A recompute window
    passes the ``min_exit_s`` of fixes before its start, and the fix before those.

    ``loaders`` are fixes of the site's loading machines around the stream's load-zone
    visits (at least ``LOADER_MATCH_S`` either side of each): a stop of at least
    ``MIN_LOAD_S`` with one within ``pairing_m`` is where the truck loaded. Pairing moves
    only the split between queue and load inside a visit, never a cycle's boundaries, so
    the resume point does not depend on it.

    Returns ``(cycles, None)`` for an empty stream.
    """
    fixes = sorted(fixes, key=lambda f: _aware(f.ts))
    if not fixes:
        return [], None

    times = [_aware(f.ts) for f in fixes]
    load_at = [_zone_at(f.lat, f.lon, load_zones) for f in fixes]
    dump_at = [_zone_at(f.lat, f.lon, dump_zones) for f in fixes]
    in_load = [z is not None for z in load_at]
    in_dump = [z is not None for z in dump_at]
    moving = [(f.speed_kph or 0.0) >= move_threshold_kph for f in fixes]
    beside = _Loaders(loaders, exclude=asset_id)

    def _long_enough(v: _Visit) -> bool:
        return (v.exit_ts - v.enter_ts).total_seconds() >= min_visit_s

    def _can_open(v: _Visit) -> bool:
        if opens_from is not None and v.enter_ts < _aware(opens_from):
            return False  # began in the lead-in: an earlier window rebuilt its cycle
        return v.start > 0 or leading_entry_known

    load_runs = _visits(in_load, times, min_exit_s)
    load_visits = [v for v in load_runs if _long_enough(v)]
    dump_visits = [v for v in _visits(in_dump, times, min_exit_s) if _long_enough(v)]

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

        queue_s, loader_id = _queue_seconds(times, fixes, moving, lv, beside, pairing_m)
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
                load_zone_id=_credited_zone(lv, load_at),
                dump_zone_id=_credited_zone(dv, dump_at),
                loader_id=loader_id,
            )
        )

    last = times[-1]
    resume = Resume(ts=last, entry_known=False)
    openers = [v for v in load_visits if _can_open(v)]
    # The last load visit, if a later fix may still extend it: the stream ends inside it,
    # or less than ``min_exit_s`` after it left (a fix back inside would rejoin it).
    tail = (
        load_runs[-1]
        if load_runs
        and (in_load[-1] or (last - load_runs[-1].exit_ts).total_seconds() < min_exit_s)
        else None
    )
    # ... and it is still too short to count.
    pending = tail if tail is not None and not _long_enough(tail) else None

    def _may_still_close(opener: _Visit) -> bool:
        if max_cycle_s is None:
            return True
        if (last - opener.enter_ts).total_seconds() < max_cycle_s:
            return True  # the next visit has not begun, and there is time for it to
        # The cap has passed, but a visit that began within it may yet count and close
        # the cycle at its entry.
        return (
            pending is not None
            and (pending.enter_ts - opener.enter_ts).total_seconds() <= max_cycle_s
        )

    if openers and _may_still_close(openers[-1]):
        # The cycle this visit opens is still in progress (its end is the next visit,
        # not yet counted) and can still finish within the cap: rebuild from its entry.
        # Everything before it is final, including the cycle that ends at this entry.
        resume = Resume(ts=openers[-1].enter_ts, entry_known=True)
    elif pending is not None and _can_open(pending):
        # A load visit has just begun and is not yet long enough to count: once it is,
        # it may open a cycle, so rebuild from its entry.
        resume = Resume(ts=pending.enter_ts, entry_known=True)
    # Otherwise no cycle is in progress (none opened, or the open one has run past the
    # cap): nothing before the last fix can change, so restart there.
    if resume.entry_known and in_load[-1] and load_runs[-1].enter_ts == resume.ts:
        resume = Resume(ts=resume.ts, entry_known=True, inside=True)
    return cycles, resume
