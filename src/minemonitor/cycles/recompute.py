"""Rebuild haul cycles and metric buckets from stored positions.

Two entry points share one per-asset routine:

* ``refresh`` — the maintenance tick. Each asset is rebuilt only from its cursor (the
  entry of the cycle still in progress, and the bucket of its newest fixes), widened back
  to any late fix that ingest marked. The cost is the open cycle plus new data, not the
  site's history (audit H3).
* ``recompute`` — on demand (API, CLI): from ``start``, or from each asset's earliest
  stored position, so a fix to the state machine can be applied retrospectively
  (brief §9).

Both rewrite exactly what a whole-history rebuild would produce for the cycles and
buckets they touch. A window starts either at a known load-zone entry, or far enough
before the changed data (``cycle_max_s`` plus the minimum visit) that no cycle the change
can affect begins earlier. Neither deletes a cycle or bucket older than its window, so
derived history outlives raw-position retention as configured (audit H4).

Assets are processed one at a time, in short transactions, and one asset's failure does
not stall the others. Rebuilders of one asset (the tick, on-demand slices) exclude each
other with a transaction-scoped advisory lock that ingest never takes. Only a rebuild
that moves the cursor also locks the cursor row, which ingest's late-fix marking waits
on: the tick's incremental run, and the newest slice of an on-demand rebuild, both
bounded by the open cycle plus new data. Older slices hold up neither ingest nor alarms.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.cycles.metrics import BUCKET_S, MetricsBucket, compute_metrics, floor_bucket
from minemonitor.cycles.statemachine import MIN_VISIT_S, Cycle, Resume, analyse_cycles
from minemonitor.storage.models import AssetMetrics, HaulCycle, Position, RecomputeCursor, Zone
from minemonitor.zones.geometry import point_in_polygon

log = logging.getLogger("minemonitor.cycles")

# Slack added to the late-data lookback, beyond what its argument needs (see _lookback).
_LOOKBACK_MARGIN_S = 60.0
# An on-demand rebuild works through history this much at a time (plus the lookback);
# no slice shorter than _CHUNK_MIN is split off.
_CHUNK = timedelta(hours=12)
_CHUNK_MIN = timedelta(hours=1)
_MIN_VISIT = timedelta(seconds=MIN_VISIT_S)


@dataclass(frozen=True)
class _Window:
    """Where one asset's rebuild starts; everything earlier is kept as stored.

    Cycles are rebuilt from ``cycles_from`` (``entry_known``: a load visit starting there
    is complete) and buckets from ``metrics_from`` (a bucket boundary).
    """

    cycles_from: datetime
    entry_known: bool
    metrics_from: datetime
    # The cursor's open load visit is still in progress: if every new fix is in the load
    # zone too, the cycles cannot have changed and only buckets are rebuilt.
    open_visit_through: datetime | None = None


@dataclass(frozen=True)
class _Zones:
    load: Zone | None
    dump: Zone | None
    geoms: list[tuple[str, dict]]
    sig: str


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _zones(session: Session, site_id: str) -> _Zones:
    zones = list(session.execute(select(Zone).where(Zone.site_id == site_id)).scalars().all())
    load = next((z for z in zones if z.kind == "loading"), None)
    dump = next((z for z in zones if z.kind == "unloading"), None)
    basis = [[z.zone_id, z.geometry] if z is not None else None for z in (load, dump)]
    sig = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
    return _Zones(load=load, dump=dump, geoms=[(z.zone_id, z.geometry) for z in zones], sig=sig)


def _lookback(max_cycle_s: float) -> timedelta:
    """How far before a late fix a rebuild starts when no load entry can anchor it.

    A cycle lasts at most ``max_cycle_s``, and whether its closing visit counts depends on
    at most ``MIN_VISIT_S`` of fixes after that visit's entry. A cycle starting further
    back than this is therefore over, and settled, before the late fix: it cannot change.
    """
    return timedelta(seconds=max_cycle_s + MIN_VISIT_S + _LOOKBACK_MARGIN_S)


def _lock_rebuild(session: Session, site_id: str, asset_id: str) -> None:
    """Exclude other rebuilders of this asset until the transaction ends.

    Two rebuilders replacing overlapping cycles concurrently would each delete what the
    other cannot yet see and both insert, duplicating rows. SQLite serialises writers.
    """
    if session.bind.dialect.name == "postgresql":
        key = f"minemonitor.recompute:{site_id}:{asset_id}"
        session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def _lock_cursor(session: Session, site_id: str, asset_id: str) -> RecomputeCursor:
    """The asset's cursor row, created if missing, locked until this transaction ends."""
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    session.execute(
        insert(RecomputeCursor)
        .values(site_id=site_id, asset_id=asset_id, entry_known=False)
        .on_conflict_do_nothing(index_elements=["site_id", "asset_id"])
    )
    return session.execute(
        select(RecomputeCursor)
        .where(RecomputeCursor.site_id == site_id, RecomputeCursor.asset_id == asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def _fix_ts(session: Session, site_id: str, asset_id: str, agg: Any) -> datetime | None:
    """``min``/``max`` device time of the asset's stored fixes."""
    ts = session.execute(
        select(agg(Position.ts)).where(Position.site_id == site_id, Position.asset_id == asset_id)
    ).scalar()
    return _aware(ts) if ts is not None else None


def _has_fix_after(session: Session, site_id: str, asset_id: str, ts: datetime | None) -> bool:
    stmt = select(Position.ts).where(Position.site_id == site_id, Position.asset_id == asset_id)
    if ts is not None:
        stmt = stmt.where(Position.ts > ts)
    return session.execute(stmt.limit(1)).first() is not None


def _start_at(session: Session, site_id: str, asset_id: str, ts: datetime) -> tuple[datetime, bool]:
    """Start a rebuild at ``ts``, or at the asset's first fix when nothing precedes ``ts``.

    The first stored fix is the start of the track, so a load visit there counts as
    complete, exactly as a whole-history rebuild treats it. Any other ``ts`` is mid-track.
    """
    first = _fix_ts(session, site_id, asset_id, func.min)
    if first is not None and first >= ts:
        return first, True
    return ts, False


def _metrics_start(
    session: Session, site_id: str, asset_id: str, start: datetime, known: bool, bucket_s: int
) -> datetime:
    """First bucket to rewrite for a rebuild starting at ``start``."""
    m = floor_bucket(start, bucket_s)
    if known and m < start:
        stored = session.execute(
            select(AssetMetrics.bucket_start).where(
                AssetMetrics.site_id == site_id,
                AssetMetrics.asset_id == asset_id,
                AssetMetrics.bucket_start == m,
            )
        ).first()
        if stored is not None:
            # ``start`` is the earliest fix left, part-way into a bucket computed before:
            # if retention has since deleted the fixes ahead of it, what is left cannot
            # rebuild it. Keep it as computed (without a cut it is the same fixes).
            m += timedelta(seconds=bucket_s)
    return m


def _window_incremental(
    session: Session,
    cur: RecomputeCursor,
    zones: _Zones,
    *,
    lookback: timedelta,
    bucket_s: int,
) -> _Window | None:
    """The tick's window for one asset, or None when nothing has changed."""
    site_id, asset_id = cur.site_id, cur.asset_id
    dirty = _aware(cur.dirty_from) if cur.dirty_from is not None else None
    if cur.cycles_from is None or cur.metrics_from is None:
        # Never computed: a new asset (ingest set dirty_from to its earliest fix), or one
        # with history from before cursors existed. Start a bounded window back.
        anchor = dirty or _fix_ts(session, site_id, asset_id, func.max)
        if anchor is None:
            return None
        start, known = _start_at(session, site_id, asset_id, anchor - lookback)
        return _Window(
            start, known, _metrics_start(session, site_id, asset_id, start, known, bucket_s)
        )

    through = _aware(cur.through_ts) if cur.through_ts is not None else None
    zones_changed = cur.zones_sig != zones.sig
    if (
        dirty is None
        and not zones_changed
        and not _has_fix_after(session, site_id, asset_id, through)
    ):
        return None

    start = _aware(cur.cycles_from)
    # After a load-zone edit the stored entry may be no entry at all.
    known = cur.entry_known and not zones_changed
    metrics_from = _aware(cur.metrics_from)
    if dirty is not None:
        if dirty < start + _MIN_VISIT:
            # The late fix may change the visit at the cursor, or a cycle ending there.
            start, known = _start_at(session, site_id, asset_id, dirty - lookback)
        # It changes its own bucket and its predecessor's (at most one bucket earlier).
        metrics_from = min(
            metrics_from, floor_bucket(dirty, bucket_s) - timedelta(seconds=bucket_s)
        )
    elif known and cur.in_open_visit and through is not None:
        return _Window(start, known, metrics_from, open_visit_through=through)
    return _Window(start, known, metrics_from)


def _window_on_demand(
    session: Session,
    site_id: str,
    asset_id: str,
    *,
    start: datetime | None,
    lookback: timedelta,
    bucket_s: int,
) -> _Window | None:
    """From ``start`` (reaching back as for late data), or from the asset's first fix."""
    if start is None:
        first = _fix_ts(session, site_id, asset_id, func.min)
        if first is None:
            return None
        s, known = first, True
    else:
        s, known = _start_at(session, site_id, asset_id, _aware(start) - lookback)
    return _Window(s, known, _metrics_start(session, site_id, asset_id, s, known, bucket_s))


def _read_fixes(
    session: Session, site_id: str, asset_id: str, since: datetime, until: datetime | None
) -> list[Any]:
    stmt = select(Position.ts, Position.lat, Position.lon, Position.speed_kph).where(
        Position.site_id == site_id, Position.asset_id == asset_id, Position.ts >= since
    )
    if until is None:
        return list(session.execute(stmt.order_by(Position.ts)).all())
    rows = list(session.execute(stmt.where(Position.ts < until).order_by(Position.ts)).all())
    # The first fix from ``until`` on ends the runs in progress there exactly as the full
    # track does, so a visit cut short by the read is not mistaken for a short one.
    after = session.execute(
        select(Position.ts, Position.lat, Position.lon, Position.speed_kph)
        .where(Position.site_id == site_id, Position.asset_id == asset_id, Position.ts >= until)
        .order_by(Position.ts)
        .limit(1)
    ).first()
    return rows + [after] if after is not None else rows


def _entry_known_at(
    session: Session, site_id: str, asset_id: str, zones: _Zones, ts: datetime, rows: list[Any]
) -> bool:
    """Whether a load visit starting at the first fix from ``ts`` on starts there.

    True unless the fix just before ``ts`` was already in the load zone.
    """
    if zones.load is None:
        return True
    before = [r for r in rows if _aware(r.ts) < ts]
    prev = (
        before[-1]
        if before
        else session.execute(
            select(Position.lat, Position.lon)
            .where(Position.site_id == site_id, Position.asset_id == asset_id, Position.ts < ts)
            .order_by(Position.ts.desc())
            .limit(1)
        ).first()
    )
    return prev is None or not point_in_polygon(prev.lat, prev.lon, zones.load.geometry)


def _still_in_open_visit(rows: list[Any], win: _Window, zones: _Zones, max_cycle_s: float) -> bool:
    """Every fix after the cursor's newest is in the load zone, within the cycle cap.

    Then the open visit simply continued: no visit began or ended, so no cycle closed,
    and the open one can still finish within the cap. Re-analysing would change nothing.
    """
    if win.open_visit_through is None or zones.load is None or not rows:
        return False
    if (_aware(rows[-1].ts) - win.cycles_from).total_seconds() >= max_cycle_s:
        return False  # the open cycle has run past the cap: let analysis retire it
    return all(
        point_in_polygon(r.lat, r.lon, zones.load.geometry)
        for r in rows
        if _aware(r.ts) > win.open_visit_through
    )


@dataclass(frozen=True)
class _Applied:
    cycles: int
    buckets: int
    last: datetime | None  # newest fix read
    resume: Resume | None  # where the next run must restart its cycles
    nxt: _Window | None  # with ``end``: where the next slice starts


def _apply(
    session: Session,
    site_id: str,
    asset_id: str,
    win: _Window,
    zones: _Zones,
    *,
    bucket_s: int,
    max_cycle_s: float,
    end: datetime | None = None,
) -> _Applied:
    """Rebuild one asset's cycles and buckets from ``win``.

    Without ``end`` the rebuild runs to the newest fix. With ``end`` (a bucket boundary)
    only cycles starting and buckets lying before it are rewritten, reading far enough
    past it that a cycle crossing it is still built whole, and the result names the
    window that continues from ``end``.
    """
    until = end + _lookback(max_cycle_s) if end is not None else None
    rows = _read_fixes(session, site_id, asset_id, win.metrics_from, until)
    rebuild_cycles = not _still_in_open_visit(rows, win, zones, max_cycle_s)
    read_from = min(win.cycles_from, win.metrics_from) if rebuild_cycles else win.metrics_from
    if read_from < win.metrics_from:
        rows = _read_fixes(session, site_id, asset_id, read_from, until)

    cycles: list[Cycle] = []
    resume: Resume | None = None
    if not rebuild_cycles:
        resume = Resume(ts=win.cycles_from, entry_known=True, inside=True)
    elif zones.load is not None and zones.dump is not None:
        cycles, resume = analyse_cycles(
            asset_id,
            [r for r in rows if _aware(r.ts) >= win.cycles_from],
            load_geom=zones.load.geometry,
            dump_geom=zones.dump.geometry,
            max_cycle_s=max_cycle_s,
            leading_entry_known=win.entry_known,
        )
        if end is not None:
            cycles = [c for c in cycles if c.start_ts < end]

    # Cycles that began before the window are kept as stored. Those ending inside it
    # still count as loads in its buckets, and no rebuilt cycle may overlap one (only
    # possible against history computed by an older version of the algorithm).
    kept_ends = [
        _aware(e)
        for e in session.execute(
            select(HaulCycle.end_ts).where(
                HaulCycle.site_id == site_id,
                HaulCycle.asset_id == asset_id,
                HaulCycle.start_ts < win.cycles_from,
                HaulCycle.end_ts >= read_from,
            )
        ).scalars()
    ]
    if rebuild_cycles:
        if kept_ends:
            overlap_until = max(kept_ends)
            cycles = [c for c in cycles if c.start_ts >= overlap_until]
        _replace_cycles(session, site_id, asset_id, cycles, win.cycles_from, end)

    buckets = compute_metrics(
        site_id,
        asset_id,
        [r for r in rows if _aware(r.ts) >= win.metrics_from],
        zones=zones.geoms,
        cycles=cycles,
        bucket_s=bucket_s,
    )
    if end is not None:
        buckets = [b for b in buckets if b.bucket_start < end]
    by_start = {b.bucket_start: b for b in buckets}
    for kept_end in kept_ends:
        b = by_start.get(floor_bucket(kept_end, bucket_s)) if kept_end >= win.metrics_from else None
        if b is not None:
            b.loads_completed += 1
    _replace_metrics(session, site_id, asset_id, buckets, win.metrics_from, end)

    if end is not None:
        known = _entry_known_at(session, site_id, asset_id, zones, end, rows)
        return _Applied(len(cycles), len(buckets), None, None, _Window(end, known, end))
    last = _aware(rows[-1].ts) if rows else None
    if last is not None and resume is None:  # no load/dump pair: no cycle in progress
        resume = Resume(ts=last, entry_known=False)
    return _Applied(len(cycles), len(buckets), last, resume, None)


def _advance(
    cur: RecomputeCursor,
    done: _Applied,
    zones: _Zones,
    *,
    bucket_s: int,
    now: datetime,
    clear_dirty: bool,
) -> None:
    """Move the cursor past a rebuild that ran to the newest fix."""
    if clear_dirty:
        cur.dirty_from = None
    cur.zones_sig = zones.sig
    cur.updated_at = now
    if done.last is not None and done.resume is not None:
        cur.cycles_from = done.resume.ts
        cur.entry_known = done.resume.entry_known
        cur.in_open_visit = done.resume.inside
        # The next run rewrites the bucket an in-order fix's predecessor sits in, and
        # the one where a cycle closes if a load visit begun within MIN_VISIT_S of
        # ``last`` turns out long enough to count.
        cur.metrics_from = floor_bucket(done.last - _MIN_VISIT, bucket_s)
        cur.through_ts = done.last


def _replace_cycles(
    session: Session,
    site_id: str,
    asset_id: str,
    cycles: list[Cycle],
    start: datetime,
    end: datetime | None,
) -> None:
    stmt = delete(HaulCycle).where(
        HaulCycle.site_id == site_id,
        HaulCycle.asset_id == asset_id,
        HaulCycle.start_ts >= start,
    )
    if end is not None:
        stmt = stmt.where(HaulCycle.start_ts < end)
    session.execute(stmt)
    for c in cycles:
        session.add(
            HaulCycle(
                site_id=site_id,
                asset_id=asset_id,
                start_ts=c.start_ts,
                end_ts=c.end_ts,
                cycle_time_s=c.cycle_time_s,
                queue_s=c.queue_s,
                load_s=c.load_s,
                haul_s=c.haul_s,
                dump_s=c.dump_s,
                return_s=c.return_s,
            )
        )


def _replace_metrics(
    session: Session,
    site_id: str,
    asset_id: str,
    buckets: list[MetricsBucket],
    start: datetime,
    end: datetime | None,
) -> None:
    stmt = delete(AssetMetrics).where(
        AssetMetrics.site_id == site_id,
        AssetMetrics.asset_id == asset_id,
        AssetMetrics.bucket_start >= start,
    )
    if end is not None:
        stmt = stmt.where(AssetMetrics.bucket_start < end)
    session.execute(stmt)
    for b in buckets:
        session.add(
            AssetMetrics(
                site_id=site_id,
                asset_id=asset_id,
                bucket_start=b.bucket_start,
                bucket_end=b.bucket_end,
                distance_m=b.distance_m,
                moving_time_s=b.moving_time_s,
                idle_time_s=b.idle_time_s,
                max_speed_kph=b.max_speed_kph,
                mean_speed_kph=b.mean_speed_kph,
                zone_dwell_s=b.zone_dwell_s,
                loads_completed=b.loads_completed,
            )
        )


@dataclass(frozen=True)
class _Ctx:
    zones: _Zones
    bucket_s: int
    max_cycle_s: float
    lookback: timedelta
    now: datetime


def _run(
    session: Session,
    site_id: str,
    asset_ids: list[str],
    rebuild: Callable[[str, _Ctx], tuple[int, int] | None],
    *,
    bucket_s: int,
    now: datetime | None,
) -> dict[str, int]:
    max_cycle_s = float(get_settings().cycle_max_s)
    ctx = _Ctx(
        zones=_zones(session, site_id),
        bucket_s=bucket_s,
        max_cycle_s=max_cycle_s,
        lookback=_lookback(max_cycle_s),
        now=now or datetime.now(UTC),
    )
    session.commit()  # end the read transaction: each asset takes its own
    out = {"cycles": 0, "buckets": 0, "assets": 0, "failed": 0}
    for asset_id in asset_ids:
        try:
            done = rebuild(asset_id, ctx)
        except Exception:  # noqa: BLE001 - one asset's failure must not stall the others
            session.rollback()
            out["failed"] += 1
            log.exception(
                "recompute failed for asset", extra={"site_id": site_id, "asset_id": asset_id}
            )
            continue
        if done is not None:
            out["cycles"] += done[0]
            out["buckets"] += done[1]
            out["assets"] += 1
    return out


def refresh(
    session: Session, site_id: str, *, bucket_s: int = BUCKET_S, now: datetime | None = None
) -> dict[str, int]:
    """Bring a site's cycles and metrics up to date with what changed since the last run.

    The maintenance tick's entry point. Per asset it rebuilds only from the cursor, or
    back to a late fix ingest marked; an asset with nothing new costs one indexed lookup.
    Returns counts of cycles and buckets rewritten, assets rebuilt and assets failed.
    """
    asset_ids = list(
        session.execute(
            select(RecomputeCursor.asset_id)
            .where(RecomputeCursor.site_id == site_id)
            .order_by(RecomputeCursor.asset_id)
        ).scalars()
    )

    def rebuild(asset_id: str, ctx: _Ctx) -> tuple[int, int] | None:
        _lock_rebuild(session, site_id, asset_id)
        cur = _lock_cursor(session, site_id, asset_id)
        win = _window_incremental(
            session, cur, ctx.zones, lookback=ctx.lookback, bucket_s=ctx.bucket_s
        )
        if win is None:
            session.rollback()  # nothing changed: release the locks
            return None
        done = _apply(
            session,
            site_id,
            asset_id,
            win,
            ctx.zones,
            bucket_s=ctx.bucket_s,
            max_cycle_s=ctx.max_cycle_s,
        )
        _advance(cur, done, ctx.zones, bucket_s=ctx.bucket_s, now=ctx.now, clear_dirty=True)
        session.commit()
        return done.cycles, done.buckets

    result = _run(session, site_id, asset_ids, rebuild, bucket_s=bucket_s, now=now)
    if result["assets"] or result["failed"]:
        log.info("recompute refreshed", extra={"site_id": site_id, **result})
    return result


def recompute(
    session: Session,
    site_id: str,
    *,
    start: datetime | None = None,
    bucket_s: int = BUCKET_S,
    now: datetime | None = None,
) -> dict[str, int]:
    """Rebuild a site's cycles and metrics from ``start`` (default: all stored positions).

    Idempotent. Reaches back before ``start`` far enough that cycles crossing it are
    rebuilt whole. Derived rows older than the window, including those whose positions
    retention has already deleted, are kept (audit H4). Each asset is rebuilt in
    ``_CHUNK`` slices, one short transaction each, so memory stays bounded and ingest
    of the asset never waits long for its cursor.
    """
    asset_ids = sorted(
        set(
            session.execute(
                select(Position.asset_id).where(Position.site_id == site_id).distinct()
            ).scalars()
        )
        | set(
            session.execute(
                select(RecomputeCursor.asset_id).where(RecomputeCursor.site_id == site_id)
            ).scalars()
        )
    )

    def rebuild(asset_id: str, ctx: _Ctx) -> tuple[int, int] | None:
        _lock_rebuild(session, site_id, asset_id)
        win = _window_on_demand(
            session, site_id, asset_id, start=start, lookback=ctx.lookback, bucket_s=ctx.bucket_s
        )
        if win is None:
            session.rollback()
            return None
        totals = [0, 0]
        while True:
            read_from = min(win.cycles_from, win.metrics_from)
            newest = _fix_ts(session, site_id, asset_id, func.max)
            # The newest slice spans at least the lookback, so the cycle still in progress
            # starts inside it and the cursor can resume from there.
            limit = newest - ctx.lookback if newest is not None else read_from
            end = None
            if limit - read_from >= _CHUNK_MIN:
                end = floor_bucket(min(read_from + _CHUNK, limit), ctx.bucket_s)
            cur = _lock_cursor(session, site_id, asset_id) if end is None else None
            done = _apply(
                session,
                site_id,
                asset_id,
                win,
                ctx.zones,
                bucket_s=ctx.bucket_s,
                max_cycle_s=ctx.max_cycle_s,
                end=end,
            )
            if cur is not None:
                # A fix ingest marked while older slices ran may lie behind this run's
                # newest fix: leave the mark for the next tick rather than lose it.
                _advance(
                    cur, done, ctx.zones, bucket_s=ctx.bucket_s, now=ctx.now, clear_dirty=False
                )
            session.commit()
            totals[0] += done.cycles
            totals[1] += done.buckets
            if done.nxt is None:
                return totals[0], totals[1]
            win = done.nxt
            _lock_rebuild(session, site_id, asset_id)

    result = _run(session, site_id, asset_ids, rebuild, bucket_s=bucket_s, now=now)
    log.info("recompute complete", extra={"site_id": site_id, **result})
    return result


def main() -> None:
    """Recompute every site from the command line (or only the site ids given)."""
    import sys

    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory
    from minemonitor.storage.repositories import served_site_ids

    settings = get_settings()
    configure_logging(settings.log_level)
    session = get_session_factory()()
    try:
        sites = sys.argv[1:] or served_site_ids(session, settings.default_site_id)
        for site_id in sites:
            result = recompute(session, site_id)
            log.info("recompute result", extra={"site_id": site_id, **result})
    finally:
        session.close()


if __name__ == "__main__":
    main()
