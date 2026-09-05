"""Rebuild haul cycles and metric buckets from stored positions.

Idempotent per window: existing rows in the window are replaced, so a fix to the
state machine can be re-applied retrospectively (brief §9). Safe to run on a
schedule or on demand.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from minemonitor.cycles.metrics import compute_metrics
from minemonitor.cycles.statemachine import compute_cycles
from minemonitor.storage.models import AssetMetrics, HaulCycle, Position, Zone

log = logging.getLogger("minemonitor.cycles")


def _zone_by_kind(zones: list[Zone], kind: str) -> Zone | None:
    return next((z for z in zones if z.kind == kind), None)


def recompute(
    session: Session,
    site_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    bucket_s: int = 300,
) -> dict[str, int]:
    """Recompute cycles and metrics for a site over an optional time window."""
    zones = list(session.execute(select(Zone).where(Zone.site_id == site_id)).scalars().all())
    load_zone = _zone_by_kind(zones, "loading")
    dump_zone = _zone_by_kind(zones, "unloading")
    zone_geoms = [(z.zone_id, z.geometry) for z in zones]

    stmt = select(Position).where(Position.site_id == site_id)
    if start is not None:
        stmt = stmt.where(Position.ts >= start)
    if end is not None:
        stmt = stmt.where(Position.ts < end)
    stmt = stmt.order_by(Position.asset_id, Position.ts)
    rows = list(session.execute(stmt).scalars().all())

    by_asset: dict[str, list[Position]] = defaultdict(list)
    for r in rows:
        by_asset[r.asset_id].append(r)

    n_cycles = n_buckets = 0
    for asset_id, fixes in by_asset.items():
        cycles = []
        if load_zone is not None and dump_zone is not None:
            cycles = compute_cycles(
                asset_id, fixes, load_geom=load_zone.geometry, dump_geom=dump_zone.geometry
            )
        buckets = compute_metrics(
            site_id, asset_id, fixes, zones=zone_geoms, cycles=cycles, bucket_s=bucket_s
        )
        _replace_cycles(session, site_id, asset_id, cycles, start, end)
        _replace_metrics(session, site_id, asset_id, buckets, start, end)
        n_cycles += len(cycles)
        n_buckets += len(buckets)

    session.commit()
    log.info(
        "recompute complete",
        extra={"site_id": site_id, "cycles": n_cycles, "buckets": n_buckets},
    )
    return {"cycles": n_cycles, "buckets": n_buckets, "assets": len(by_asset)}


def _replace_cycles(session, site_id, asset_id, cycles, start, end) -> None:
    stmt = delete(HaulCycle).where(HaulCycle.site_id == site_id, HaulCycle.asset_id == asset_id)
    if start is not None:
        stmt = stmt.where(HaulCycle.start_ts >= start)
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


def _replace_metrics(session, site_id, asset_id, buckets, start, end) -> None:
    stmt = delete(AssetMetrics).where(
        AssetMetrics.site_id == site_id, AssetMetrics.asset_id == asset_id
    )
    if start is not None:
        stmt = stmt.where(AssetMetrics.bucket_start >= start)
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


def main() -> None:
    """Recompute the default site from the command line."""
    from minemonitor.config import get_settings
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory

    settings = get_settings()
    configure_logging(settings.log_level)
    session = get_session_factory()()
    try:
        result = recompute(session, settings.default_site_id)
        log.info("recompute result", extra=result)
    finally:
        session.close()


if __name__ == "__main__":
    main()
