"""Trends — the same shift scorecard, walked back over recent shifts.

Answers "is it getting better or worse?" by lining up the last N shifts' observed
numbers. It reuses ``compute_scorecard`` per window, so a trend is only ever a
series of already-observed values — never a projection or a fitted line. No
targets, no benchmarks (brief §9): the series is the evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from minemonitor.operations.scorecard import compute_scorecard
from minemonitor.operations.shifts import ShiftWindow, resolve_shift


def recent_windows(session: Session, site_id: str, at: datetime, count: int) -> list[ShiftWindow]:
    """The ``count`` shift windows ending with the one containing ``at``, newest first."""
    windows: list[ShiftWindow] = []
    cursor = at
    seen: set[str] = set()
    # Guard the loop: at most a few probes per wanted window (shifts are hours long).
    for _ in range(count * 4):
        if len(windows) >= count:
            break
        window = resolve_shift(session, site_id, cursor)
        if window is None:
            # Fell into a gap between shifts; step back and keep looking.
            cursor = cursor - timedelta(hours=1)
            continue
        if window.shift_id not in seen:
            windows.append(window)
            seen.add(window.shift_id)
        cursor = window.start - timedelta(seconds=1)
    return windows


def compute_trends(session: Session, site_id: str, at: datetime, count: int = 7) -> dict[str, Any]:
    """A per-shift series of the headline metrics over the last ``count`` shifts.

    Newest first. Each point is the observed scorecard for that shift, flattened to
    the figures a trend cares about.
    """
    points: list[dict[str, Any]] = []
    for window in recent_windows(session, site_id, at, count):
        sc = compute_scorecard(session, site_id, window)
        points.append(
            {
                "shift_id": window.shift_id,
                "name": window.name,
                "operating_date": window.operating_date.isoformat(),
                "start": window.start,
                "cycles": sc["cycles"]["count"],
                "mean_cycle_time_s": sc["cycles"]["mean_cycle_time_s"],
                "queue_pct": sc["cycles"]["queue_pct"],
                "utilisation_pct": sc["utilisation"]["utilisation_pct"],
                "safety_events": sc["safety_events"]["total"],
                "classified_delay_s": sc["delays"]["classified_total_s"],
                "delay_by_category": sc["delays"]["by_category"],
                "incidents_opened": sc["incidents"]["opened_this_shift"],
            }
        )
    return {"site_id": site_id, "shifts": count, "series": points}
