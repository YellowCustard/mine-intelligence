"""Live state for the dashboard: a snapshot endpoint and an SSE stream.

Server-Sent Events, one-way server→browser, chosen over WebSockets because they
are simpler and survive flaky links (brief §7). The stream polls the database on
a short interval rather than coupling the API to MQTT — robust regardless of how
positions arrived, and cheap at Phase-1 volumes (~9 assets).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.auth.deps import require_viewer
from minemonitor.cycles.shifts import shift_bounds
from minemonitor.events.repository import list_events
from minemonitor.storage.db import get_db, get_session_factory
from minemonitor.storage.models import Asset, HaulCycle, Zone
from minemonitor.storage.repositories import latest_positions

router = APIRouter(tags=["stream"])

_SEGMENTS = ("queue_s", "load_s", "haul_s", "dump_s", "return_s")


def _current_shift(now: datetime) -> tuple[datetime, datetime, str]:
    """The shift window (UTC) containing ``now``, in Africa/Harare."""
    local_date = now.date()
    for shift in ("day", "night"):
        start, end = shift_bounds(local_date, shift)
        if start <= now < end:
            return start, end, shift
    # Before the day shift starts: we are in the previous day's night shift.
    from datetime import timedelta

    start, end = shift_bounds(local_date - timedelta(days=1), "night")
    return start, end, "night"


def _cycles_in(session: Session, site_id: str, start: datetime, end: datetime) -> list[HaulCycle]:
    return list(
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


def _cycle_summary(session: Session, site_id: str, now: datetime) -> dict[str, Any]:
    from datetime import timedelta

    start, end, shift = _current_shift(now)
    rows = _cycles_in(session, site_id, start, end)
    # If the current shift has just begun (or rolled over) with no complete
    # cycles yet, fall back to the previous shift so the chart still informs.
    if not rows:
        prev_start = start - timedelta(hours=12)
        prev_rows = _cycles_in(session, site_id, prev_start, start)
        if prev_rows:
            rows, shift = prev_rows, ("night" if shift == "day" else "day")
    n = len(rows)
    if n == 0:
        return {"shift": shift, "cycles": 0, "segments_s": {}, "queue_pct": None}
    total = sum(c.cycle_time_s for c in rows)
    seg = {s: sum(getattr(c, s) for c in rows) for s in _SEGMENTS}
    return {
        "shift": shift,
        "cycles": n,
        "mean_cycle_time_s": total / n,
        "segments_s": {s: seg[s] / n for s in _SEGMENTS},
        "queue_pct": (100.0 * seg["queue_s"] / total) if total else 0.0,
    }


def _snapshot(session: Session, site_id: str, *, include_zones: bool) -> dict[str, Any]:
    now = datetime.now(UTC)
    classes = {
        a.asset_id: a.asset_class
        for a in session.execute(select(Asset).where(Asset.site_id == site_id)).scalars()
    }
    assets = [
        {
            "asset_id": p.asset_id,
            "asset_class": classes.get(p.asset_id, "unknown"),
            "lat": p.lat,
            "lon": p.lon,
            "speed_kph": p.speed_kph,
            "heading_deg": p.heading_deg,
            "ignition": p.ignition,
            "ts": p.ts,
        }
        for p in latest_positions(session, site_id)
    ]
    events = [
        {
            "event_id": e.event_id,
            "ts": e.ts,
            "type": e.type,
            "severity": e.severity,
            "asset_id": e.asset_id,
            "zone_id": e.zone_id,
            "source": e.source,
            "summary": e.summary,
            "state": e.state,
        }
        for e in list_events(session, site_id, limit=100)
        if e.state != "resolved"
    ]
    snap: dict[str, Any] = {
        "site_id": site_id,
        "now": now,
        "assets": assets,
        "events": events,
        "cycles": _cycle_summary(session, site_id, now),
    }
    if include_zones:
        snap["zones"] = [
            {
                "zone_id": z.zone_id,
                "name": z.name,
                "kind": z.kind,
                "geometry": z.geometry,
                "rules": z.rules,
            }
            for z in session.execute(select(Zone).where(Zone.site_id == site_id)).scalars()
        ]
    return snap


@router.get("/sites/{site_id}/state", dependencies=[Depends(require_viewer)])
def state(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One-shot snapshot for initial dashboard load (includes zones)."""
    return _snapshot(db, site_id, include_zones=True)


@router.get("/sites/{site_id}/stream", dependencies=[Depends(require_viewer)])
def stream(site_id: str, poll_s: float = Query(default=2.0, ge=0.5, le=30)) -> StreamingResponse:
    """SSE stream of live state (assets, events, cycle summary)."""

    def gen():
        factory = get_session_factory()
        try:
            while True:
                session = factory()
                try:
                    payload = _snapshot(session, site_id, include_zones=False)
                finally:
                    session.close()
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                time.sleep(poll_s)
        except GeneratorExit:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
