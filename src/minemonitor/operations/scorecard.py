"""Shift scorecard — the one-glance answer to "how did this shift go?".

Every number here is **observed or derived from stored data**, recomputable from
source, and carries no target or benchmark — the brief is explicit that we report
this mine's real numbers, never a demo figure (§9). The only comparison offered is
against the *previous* shift (an observed delta), not against an invented goal.

Reuses what already exists: haul cycles (queue/cycle analytics), the per-bucket
``AssetMetrics`` rollup (moving/idle time → utilisation), the alarm queue
(``Event``), and the PR-B annotation tables (``Incident``, ``DelayClassification``).
Nothing here writes; it is a pure read over the shift window.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.operations.shifts import ShiftWindow, resolve_shift
from minemonitor.storage.models import (
    AssetMetrics,
    DelayClassification,
    Event,
    HaulCycle,
    Incident,
)

_SEGMENTS = ("queue_s", "load_s", "haul_s", "dump_s", "return_s")
_OPEN_INCIDENT_STATES = ("open", "acknowledged", "investigating", "assigned")


def _aware(ts: datetime) -> datetime:
    """Treat a naive timestamp as UTC. SQLite drops tzinfo on read; Postgres keeps
    it — this makes the in-Python window arithmetic safe on both."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _cycles_summary(rows: list[HaulCycle]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"count": 0, "mean_cycle_time_s": None, "queue_pct": None, "segment_pct": {}}
    total = sum(c.cycle_time_s for c in rows)
    seg = {s: sum(getattr(c, s) for c in rows) for s in _SEGMENTS}
    return {
        "count": n,
        "mean_cycle_time_s": total / n,
        "queue_pct": (100.0 * seg["queue_s"] / total) if total else 0.0,
        "segment_pct": {s: (100.0 * seg[s] / total if total else 0.0) for s in _SEGMENTS},
    }


def _utilisation(rows: list[AssetMetrics]) -> dict[str, Any]:
    """Moving vs idle time from the per-bucket rollup. Derived, not a raw fact."""
    moving = sum(m.moving_time_s for m in rows)
    idle = sum(m.idle_time_s for m in rows)
    denom = moving + idle
    return {
        "moving_time_s": moving,
        "idle_time_s": idle,
        "utilisation_pct": (100.0 * moving / denom) if denom else None,
        "basis": "derived",  # from AssetMetrics buckets, not a measured availability
    }


def compute_scorecard(session: Session, site_id: str, window: ShiftWindow) -> dict[str, Any]:
    """The full scorecard for one shift window. All numbers observed/derived."""
    start, end = window.start, window.end

    cycles = list(
        session.execute(
            select(HaulCycle).where(
                HaulCycle.site_id == site_id,
                HaulCycle.start_ts >= start,
                HaulCycle.start_ts < end,
            )
        )
        .scalars()
        .all()
    )
    metrics = list(
        session.execute(
            select(AssetMetrics).where(
                AssetMetrics.site_id == site_id,
                AssetMetrics.bucket_start >= start,
                AssetMetrics.bucket_start < end,
            )
        )
        .scalars()
        .all()
    )
    events = list(
        session.execute(
            select(Event).where(Event.site_id == site_id, Event.ts >= start, Event.ts < end)
        )
        .scalars()
        .all()
    )
    severity: dict[str, int] = defaultdict(int)
    for e in events:
        severity[e.severity] += 1

    # Delays overlapping the window; total classified downtime and a per-category
    # breakdown. Clipped to the window so a delay spanning the boundary counts only
    # its in-window seconds.
    delays = list(
        session.execute(
            select(DelayClassification).where(
                DelayClassification.site_id == site_id,
                DelayClassification.end_ts > start,
                DelayClassification.start_ts < end,
            )
        )
        .scalars()
        .all()
    )
    by_category: dict[str, float] = defaultdict(float)
    total_delay_s = 0.0
    for d in delays:
        overlap = (min(_aware(d.end_ts), end) - max(_aware(d.start_ts), start)).total_seconds()
        if overlap > 0:
            by_category[d.category] += overlap
            total_delay_s += overlap

    # Incidents: those still open now, and those opened during the shift.
    open_incidents = (
        session.execute(
            select(Incident).where(
                Incident.site_id == site_id, Incident.state.in_(_OPEN_INCIDENT_STATES)
            )
        )
        .scalars()
        .all()
    )
    opened_in_shift = (
        session.execute(
            select(Incident).where(
                Incident.site_id == site_id,
                Incident.created_at >= start,
                Incident.created_at < end,
            )
        )
        .scalars()
        .all()
    )

    return {
        "shift": window.as_dict(),
        "cycles": _cycles_summary(cycles),
        "utilisation": _utilisation(metrics),
        "safety_events": {"total": len(events), "by_severity": dict(severity)},
        "delays": {
            "classified_total_s": total_delay_s,
            "by_category": dict(by_category),
        },
        "incidents": {
            "open_now": len(list(open_incidents)),
            "opened_this_shift": len(list(opened_in_shift)),
        },
    }


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def scorecard_with_comparison(session: Session, site_id: str, at: datetime) -> dict[str, Any]:
    """The scorecard for the shift containing ``at``, plus a delta vs the previous
    shift. Returns ``{"shift": null}`` if ``at`` falls outside all shifts.
    """
    window = resolve_shift(session, site_id, at)
    if window is None:
        return {"shift": None}
    current = compute_scorecard(session, site_id, window)

    # The previous shift is the one containing the instant just before this one.
    prev_window = resolve_shift(session, site_id, window.start - timedelta(seconds=1))
    comparison: dict[str, Any] | None = None
    if prev_window is not None and prev_window.shift_id != window.shift_id:
        previous = compute_scorecard(session, site_id, prev_window)
        comparison = {
            "previous_shift_id": prev_window.shift_id,
            "queue_pct_delta": _delta(
                current["cycles"]["queue_pct"], previous["cycles"]["queue_pct"]
            ),
            "mean_cycle_time_s_delta": _delta(
                current["cycles"]["mean_cycle_time_s"],
                previous["cycles"]["mean_cycle_time_s"],
            ),
            "utilisation_pct_delta": _delta(
                current["utilisation"]["utilisation_pct"],
                previous["utilisation"]["utilisation_pct"],
            ),
            "cycles_delta": current["cycles"]["count"] - previous["cycles"]["count"],
        }
    return {**current, "comparison": comparison}
