"""Data-quality monitoring — how much to trust the telemetry right now.

Every derived number in the platform is only as good as the fixes underneath it,
so this inspects recent positions for the failure modes a remote GNSS fleet
actually shows: stale feeds, late/backfilled fixes, out-of-order arrivals,
physically impossible jumps, and missing quality fields. It never mutates
telemetry — it reads and reports, and rolls the counts into a plain confidence
label so a reader knows whether to trust the shift's numbers.

A stale feed is called out as a *data* problem, not machine downtime (see the
exception layer): the two are never conflated.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from minemonitor.ingest.geo import haversine_m
from minemonitor.storage.models import Position
from minemonitor.storage.repositories import list_positions

# Thresholds. Deliberately generous — we flag the clearly wrong, not GNSS jitter.
_BACKFILL_LAG_S = 120.0  # received this long after the fix → late/backfilled
_CLOCK_SKEW_S = 120.0  # device ts this far AHEAD of server receipt → bad device clock
_IMPOSSIBLE_KPH = 160.0  # implied ground speed above this for a mine machine → suspect
_SAMPLE = 1000  # most-recent positions inspected


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def compute_data_quality(
    session: Session, site_id: str, now: datetime, offline_after_s: float
) -> dict[str, Any]:
    positions = list_positions(session, site_id, limit=_SAMPLE)
    sample = len(positions)

    by_asset: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_asset[p.asset_id].append(p)

    stale_feeds: list[str] = []
    late_fixes = 0
    future_dated = 0
    out_of_order = 0
    impossible_moves = 0
    missing_fields = 0

    for p in positions:
        if p.received_at is not None and p.ts is not None:
            # received_at is server-stamped at ingest; ts is device time. A large
            # positive gap is late/backfilled data; a large NEGATIVE gap (device
            # ahead of the server) is a bad device clock that would silently
            # misplace fixes into the wrong shift — flag it rather than trust it.
            lag = (_aware(p.received_at) - _aware(p.ts)).total_seconds()
            if lag > _BACKFILL_LAG_S:
                late_fixes += 1
            elif lag < -_CLOCK_SKEW_S:
                future_dated += 1
        if p.hdop is None or p.satellites is None:
            missing_fields += 1

    for asset_id, rows in by_asset.items():
        # list_positions returns newest-first; walk oldest-first per asset.
        ordered = sorted(rows, key=lambda r: _aware(r.ts))
        latest_age = (now - _aware(ordered[-1].ts)).total_seconds()
        if latest_age > offline_after_s:
            stale_feeds.append(asset_id)
        prev: Position | None = None
        for cur in ordered:
            if prev is not None:
                dt = (_aware(cur.ts) - _aware(prev.ts)).total_seconds()
                if dt <= 0:
                    out_of_order += 1
                else:
                    dist = haversine_m(prev.lat, prev.lon, cur.lat, cur.lon)
                    implied_kph = (dist / dt) * 3.6
                    if implied_kph > _IMPOSSIBLE_KPH:
                        impossible_moves += 1
            prev = cur

    issues = late_fixes + future_dated + out_of_order + impossible_moves + missing_fields
    # A plain confidence label from the issue rate over the sample. Not a score to
    # optimise — a signal for how much to trust the derived numbers.
    rate = (issues / sample) if sample else 0.0
    if sample == 0:
        confidence = "unknown"
    elif rate < 0.02 and not stale_feeds:
        confidence = "good"
    elif rate < 0.10:
        confidence = "fair"
    else:
        confidence = "poor"

    return {
        "site_id": site_id,
        "sample_size": sample,
        "confidence": confidence,
        "issues": {
            "stale_feeds": len(stale_feeds),
            "late_or_backfilled_fixes": late_fixes,
            "future_dated_fixes": future_dated,
            "out_of_order_fixes": out_of_order,
            "impossible_moves": impossible_moves,
            "missing_quality_fields": missing_fields,
        },
        "stale_feed_assets": sorted(stale_feeds),
        "note": "Stale feeds are a data problem, not machine downtime.",
    }
