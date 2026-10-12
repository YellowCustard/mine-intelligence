"""Live state for the dashboard: a snapshot endpoint and an SSE stream.

Server-Sent Events, one-way server→browser, chosen over WebSockets because they
are simpler and survive flaky links (brief §7). The stream polls the database on
a short interval rather than coupling the API to MQTT — robust regardless of how
positions arrived.

The stream waits between frames on the event loop, not in a worker thread, so an open
dashboard holds no thread: sync routes share AnyIO's small worker pool (40 threads), and
a stream that slept in one used to keep it for as long as the tab stayed open. Every
open stream for a site shares one snapshot, computed at most once per
``_SNAPSHOT_MAX_AGE_S``, so ten dashboards cost the database what one does.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.auth.deps import require_viewer
from minemonitor.config import get_settings
from minemonitor.events.repository import list_events
from minemonitor.operations import equipment
from minemonitor.operations.shifts import resolve_shift
from minemonitor.storage.db import get_db, get_session_factory
from minemonitor.storage.models import Asset, HaulCycle, Zone
from minemonitor.storage.repositories import latest_positions

router = APIRouter(tags=["stream"])

_SEGMENTS = ("queue_s", "load_s", "haul_s", "dump_s", "return_s")


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
    """Cycle breakdown for the site's shift containing ``now``, from its own shift
    definitions (the same window the header shows), never a fixed day/night split."""
    from datetime import timedelta

    window = resolve_shift(session, site_id, now)
    if window is None:
        return {"shift": None, "shift_id": None, "cycles": 0, "segments_s": {}, "queue_pct": None}
    rows = _cycles_in(session, site_id, window.start, window.end)
    # If the current shift has just begun (or rolled over) with no complete
    # cycles yet, fall back to the previous shift so the chart still informs.
    if not rows:
        prev = resolve_shift(session, site_id, window.start - timedelta(seconds=1))
        if prev is not None and prev.shift_id != window.shift_id:
            prev_rows = _cycles_in(session, site_id, prev.start, prev.end)
            if prev_rows:
                rows, window = prev_rows, prev
    n = len(rows)
    if n == 0:
        return {
            "shift": window.name,
            "shift_id": window.shift_id,
            "cycles": 0,
            "segments_s": {},
            "queue_pct": None,
        }
    total = sum(c.cycle_time_s for c in rows)
    seg = {s: sum(getattr(c, s) for c in rows) for s in _SEGMENTS}
    return {
        "shift": window.name,
        "shift_id": window.shift_id,
        "cycles": n,
        "mean_cycle_time_s": total / n,
        "segments_s": {s: seg[s] / n for s in _SEGMENTS},
        "queue_pct": (100.0 * seg["queue_s"] / total) if total else 0.0,
    }


def _asset_snapshot(
    p: Any, asset_class: str, now: datetime, offline_after_s: float
) -> dict[str, Any]:
    status = equipment.derive_state(
        latest_ts=p.ts,
        speed_kph=p.speed_kph,
        ignition=p.ignition,
        now=now,
        offline_after_s=offline_after_s,
    )
    return {
        "asset_id": p.asset_id,
        "asset_class": asset_class,
        "lat": p.lat,
        "lon": p.lon,
        "speed_kph": p.speed_kph,
        "heading_deg": p.heading_deg,
        "ignition": p.ignition,
        "ts": p.ts,
        # Derived equipment state; state_basis says whether it is observed or inferred.
        "state": status.state,
        "state_basis": status.basis,
        "data_age_s": status.data_age_s,
    }


def _snapshot(session: Session, site_id: str, *, include_zones: bool) -> dict[str, Any]:
    now = datetime.now(UTC)
    offline_after_s = get_settings().offline_threshold_s
    classes = {
        a.asset_id: a.asset_class
        for a in session.execute(select(Asset).where(Asset.site_id == site_id)).scalars()
    }
    assets = [
        _asset_snapshot(p, classes.get(p.asset_id, "unknown"), now, offline_after_s)
        for p in latest_positions(session, site_id)
    ]
    shift = resolve_shift(session, site_id, now)
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
            # How the alarm's conclusion was reached (measured / inferred / vendor_inferred …),
            # rendered beside the source so a camera guess is never shown as a measurement.
            "provenance": (e.detail or {}).get("provenance"),
        }
        for e in list_events(session, site_id, limit=100)
        if e.state != "resolved"
    ]
    snap: dict[str, Any] = {
        "site_id": site_id,
        "now": now,
        "shift": shift.as_dict() if shift is not None else None,
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


# A frame may be this old when it is sent. Below the stream's shortest poll interval it
# adds no visible lag; above it, frames would repeat.
_SNAPSHOT_MAX_AGE_S = 0.5
# A site no stream has asked about for this long is dropped from the cache.
_SNAPSHOT_IDLE_S = 60.0


class _Snapshots:
    """The latest SSE frame per site, shared by every open stream for that site.

    ``get`` runs in a worker thread. At most one thread computes a site's frame at a time;
    another stream asking meanwhile gets the previous frame instead of queueing behind it,
    so a slow snapshot ties up one thread, not one per open dashboard.
    """

    def __init__(self, max_age_s: float, idle_s: float) -> None:
        self._max_age_s = max_age_s
        self._idle_s = idle_s
        self._guard = threading.Lock()
        self._frames: dict[str, tuple[float, str]] = {}  # site -> (monotonic time, frame)
        self._computing: dict[str, threading.Lock] = {}

    def fresh(self, site_id: str) -> str | None:
        """The cached frame if it is still fresh enough to send; safe on the event loop."""
        with self._guard:
            hit = self._frames.get(site_id)
        if hit is not None and time.monotonic() - hit[0] < self._max_age_s:
            return hit[1]
        return None

    def get(self, site_id: str) -> str:
        """A fresh frame, computing it if needed. Blocks; call it from a worker thread."""
        with self._guard:
            lock = self._computing.setdefault(site_id, threading.Lock())
            stale = self._frames.get(site_id)
        if not lock.acquire(blocking=False):
            if stale is not None:
                return stale[1]  # another stream is computing its replacement
            lock.acquire()  # nothing to send yet: wait for the frame being computed
        try:
            frame = self.fresh(site_id)  # computed while this thread waited for the lock
            if frame is None:
                frame = _frame(site_id)
                with self._guard:
                    now = time.monotonic()
                    self._frames[site_id] = (now, frame)
                    for sid, (at, _) in list(self._frames.items()):
                        if now - at > self._idle_s:
                            del self._frames[sid]
                            self._computing.pop(sid, None)
            return frame
        finally:
            lock.release()


def _frame(site_id: str) -> str:
    session = get_session_factory()()
    try:
        payload = _snapshot(session, site_id, include_zones=False)
    finally:
        session.close()
    return f"data: {json.dumps(payload, default=str)}\n\n"


_SNAPSHOTS = _Snapshots(_SNAPSHOT_MAX_AGE_S, _SNAPSHOT_IDLE_S)


@router.get("/sites/{site_id}/stream", dependencies=[Depends(require_viewer)])
def stream(site_id: str, poll_s: float = Query(default=2.0, ge=0.5, le=30)) -> StreamingResponse:
    """SSE stream of live state (assets, events, cycle summary)."""

    async def gen() -> AsyncIterator[str]:
        while True:
            frame = _SNAPSHOTS.fresh(site_id)
            if frame is None:
                frame = await anyio.to_thread.run_sync(_SNAPSHOTS.get, site_id)
            yield frame
            await anyio.sleep(poll_s)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
