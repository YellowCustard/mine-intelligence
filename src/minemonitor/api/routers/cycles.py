"""Haul-cycle analytics API: cycles, per-shift summary, metrics, and recompute.

No target or benchmark is ever returned — only this mine's observed numbers
(brief §9).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.cycles.recompute import recompute
from minemonitor.operations.shifts import resolve_shift_by_id
from minemonitor.storage.db import get_db
from minemonitor.storage.models import AssetMetrics, HaulCycle, User

router = APIRouter(tags=["cycles"])

_SEGMENTS = ("queue_s", "load_s", "haul_s", "dump_s", "return_s")


def _cycle_dict(c: HaulCycle) -> dict[str, Any]:
    d = {
        "site_id": c.site_id,
        "asset_id": c.asset_id,
        "start_ts": c.start_ts,
        "end_ts": c.end_ts,
        "cycle_time_s": c.cycle_time_s,
        **{s: getattr(c, s) for s in _SEGMENTS},
    }
    d["queue_pct"] = 100.0 * c.queue_s / c.cycle_time_s if c.cycle_time_s else 0.0
    return d


@router.get("/sites/{site_id}/assets/{asset_id}/cycles", dependencies=[Depends(require_viewer)])
def get_cycles(
    site_id: str,
    asset_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(HaulCycle).where(HaulCycle.site_id == site_id, HaulCycle.asset_id == asset_id)
    if since is not None:
        stmt = stmt.where(HaulCycle.start_ts >= since)
    if until is not None:
        stmt = stmt.where(HaulCycle.start_ts < until)
    stmt = stmt.order_by(HaulCycle.start_ts).limit(limit)
    return [_cycle_dict(c) for c in db.execute(stmt).scalars().all()]


def _summarise(cycles: list[HaulCycle]) -> dict[str, Any]:
    n = len(cycles)
    if n == 0:
        return {"cycles": 0}
    total_cycle = sum(c.cycle_time_s for c in cycles)
    seg_totals = {s: sum(getattr(c, s) for c in cycles) for s in _SEGMENTS}
    return {
        "cycles": n,
        "mean_cycle_time_s": total_cycle / n,
        "mean_segment_s": {s: seg_totals[s] / n for s in _SEGMENTS},
        "segment_pct": {
            s: (100.0 * seg_totals[s] / total_cycle if total_cycle else 0.0) for s in _SEGMENTS
        },
    }


@router.get("/sites/{site_id}/shift-summary", dependencies=[Depends(require_viewer)])
def shift_summary(
    site_id: str,
    shift_date: date = Query(alias="date"),
    shift: str = Query(default="day"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-asset and site-wide cycle summary for a shift: the site's own shift ``shift``
    starting on local date ``date``. A shift the site does not run is a 404."""
    window = resolve_shift_by_id(db, site_id, f"{site_id}:{shift_date.isoformat()}:{shift}")
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} has no shift {shift!r}")
    start, end = window.start, window.end
    rows = list(
        db.execute(
            select(HaulCycle)
            .where(
                HaulCycle.site_id == site_id,
                HaulCycle.start_ts >= start,
                HaulCycle.start_ts < end,
            )
            .order_by(HaulCycle.asset_id, HaulCycle.start_ts)
        )
        .scalars()
        .all()
    )
    by_asset: dict[str, list[HaulCycle]] = {}
    for r in rows:
        by_asset.setdefault(r.asset_id, []).append(r)
    return {
        "site_id": site_id,
        "shift": shift,
        "shift_id": window.shift_id,
        "date": shift_date.isoformat(),
        "start": start,
        "end": end,
        "fleet": _summarise(rows),
        "by_asset": {a: _summarise(cs) for a, cs in by_asset.items()},
    }


@router.get("/sites/{site_id}/metrics", dependencies=[Depends(require_viewer)])
def get_metrics(
    site_id: str,
    asset_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(AssetMetrics).where(AssetMetrics.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(AssetMetrics.asset_id == asset_id)
    if since is not None:
        stmt = stmt.where(AssetMetrics.bucket_start >= since)
    if until is not None:
        stmt = stmt.where(AssetMetrics.bucket_start < until)
    stmt = stmt.order_by(AssetMetrics.bucket_start.desc()).limit(limit)
    return [
        {
            "schema": "asset.metrics.v1",
            "site_id": m.site_id,
            "asset_id": m.asset_id,
            "bucket_start": m.bucket_start,
            "bucket_end": m.bucket_end,
            "distance_m": m.distance_m,
            "moving_time_s": m.moving_time_s,
            "idle_time_s": m.idle_time_s,
            "max_speed_kph": m.max_speed_kph,
            "mean_speed_kph": m.mean_speed_kph,
            "zone_dwell_s": m.zone_dwell_s,
            "loads_completed": m.loads_completed,
        }
        for m in db.execute(stmt).scalars().all()
    ]


@router.post("/sites/{site_id}/recompute")
def trigger_recompute(
    site_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, int]:
    """Rebuild cycles and metrics from stored positions (idempotent).

    ``since`` limits the rebuild to cycles and buckets from shortly before it onwards;
    omitted, everything still backed by stored positions is rebuilt. A rebuild always
    runs to the newest data: ``until`` is refused rather than ignored, because cutting a
    window short would split the cycle crossing its end.
    """
    if until is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "until is not supported: a recompute runs from since to the newest data",
        )
    result = recompute(db, site_id, start=since)
    audit.record(
        db,
        actor=user.username,
        action="recompute",
        entity_type="site",
        entity_id=site_id,
        site_id=site_id,
        detail=result,
    )
    db.commit()
    return result
