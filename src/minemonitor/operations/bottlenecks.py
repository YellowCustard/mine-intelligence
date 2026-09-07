"""Bottleneck observations — where time appears to be going, stated honestly.

This is the most misusable analytic in the platform, so it is deliberately
conservative: it reports **observations and correlations**, each with the evidence
it is drawn from and an explicit ``causal: false``. It never says "the loader
caused the queue" — only "queue was the largest share of cycle time this shift
(evidence: …)". A supervisor draws the conclusion; the system supplies the numbers.

All figures come from stored haul cycles and delay classifications for a shift
window; nothing here writes or infers a hidden value.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.operations.scorecard import compute_scorecard
from minemonitor.operations.shifts import ShiftWindow
from minemonitor.storage.models import HaulCycle

_SEGMENTS = ("queue_s", "load_s", "haul_s", "dump_s", "return_s")
# How far above the fleet mean a truck's mean cycle must sit to be flagged an outlier.
_OUTLIER_RATIO = 1.25


def _observation(kind: str, summary: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "summary": summary, "evidence": evidence, "causal": False}


def compute_bottlenecks(session: Session, site_id: str, window: ShiftWindow) -> dict[str, Any]:
    """Observations for a shift window. Empty ``observations`` means nothing stood out."""
    cycles = list(
        session.execute(
            select(HaulCycle).where(
                HaulCycle.site_id == site_id,
                HaulCycle.start_ts >= window.start,
                HaulCycle.start_ts < window.end,
            )
        )
        .scalars()
        .all()
    )
    observations: list[dict[str, Any]] = []

    if cycles:
        total = sum(c.cycle_time_s for c in cycles)
        seg_totals = {s: sum(getattr(c, s) for c in cycles) for s in _SEGMENTS}
        if total > 0:
            largest = max(seg_totals, key=lambda s: seg_totals[s])
            pct = 100.0 * seg_totals[largest] / total
            observations.append(
                _observation(
                    "segment_share",
                    f"{largest.removesuffix('_s')} was the largest share of cycle time "
                    f"at {pct:.0f}% across {len(cycles)} cycles",
                    {"segment": largest, "share_pct": pct, "cycles": len(cycles)},
                )
            )

        # Per-asset mean cycle vs fleet mean — outlier trucks (a correlation, not a cause).
        by_asset: dict[str, list[float]] = {}
        for c in cycles:
            by_asset.setdefault(c.asset_id, []).append(c.cycle_time_s)
        fleet_mean = total / len(cycles)
        for asset_id, times in sorted(by_asset.items()):
            asset_mean = sum(times) / len(times)
            if fleet_mean > 0 and asset_mean >= _OUTLIER_RATIO * fleet_mean:
                observations.append(
                    _observation(
                        "slow_asset",
                        f"{asset_id} mean cycle {asset_mean / 60:.1f} min is "
                        f"{asset_mean / fleet_mean:.2f}x the fleet mean",
                        {
                            "asset_id": asset_id,
                            "asset_mean_s": asset_mean,
                            "fleet_mean_s": fleet_mean,
                            "cycles": len(times),
                        },
                    )
                )

    # The largest classified downtime category, if any was recorded.
    sc = compute_scorecard(session, site_id, window)
    by_cat = sc["delays"]["by_category"]
    if by_cat:
        top = max(by_cat, key=lambda k: by_cat[k])
        observations.append(
            _observation(
                "top_downtime",
                f"most classified downtime was '{top}' ({by_cat[top] / 60:.0f} min)",
                {"category": top, "seconds": by_cat[top]},
            )
        )

    return {
        "shift": window.as_dict(),
        "observations": observations,
        "note": "Observations and correlations only — not statements of cause.",
    }
