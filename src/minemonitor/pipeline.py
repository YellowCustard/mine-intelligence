"""Per-position processing: zones → debounce → rules → events.

Called for each newly-stored position. Runs in the caller's transaction so the
position, its updated zone state, and any events it raises commit atomically —
a crash cannot leave a stored position whose events were lost.

Only *new* positions are processed (``created=True``): replaying a position must
not double-fire events (brief §12). Out-of-order/late fixes are stored but skipped
for live rule evaluation — they remain available for retrospective recompute.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.contracts.position import AssetPositionV1
from minemonitor.events.repository import persist_event
from minemonitor.rules.config import parse_rules
from minemonitor.rules.evaluate import on_entry, on_inzone_fix
from minemonitor.storage.models import Asset
from minemonitor.zones import engine
from minemonitor.zones.geometry import distance_to_polygon_m, point_in_polygon
from minemonitor.zones.repository import get_or_create_state, list_zones


def _asset_class(session: Session, site_id: str, asset_id: str) -> str:
    row = session.execute(
        select(Asset.asset_class).where(Asset.site_id == site_id, Asset.asset_id == asset_id)
    ).first()
    return row[0] if row else "unknown"


def process_position(session: Session, pos: AssetPositionV1, created: bool) -> list[EventV1]:
    """Evaluate zones and rules for one position. Adds events; does not commit."""
    if not created:
        return []
    zones = list_zones(session, pos.site_id)
    if not zones:
        return []

    asset_class = _asset_class(session, pos.site_id, pos.asset_id)
    events: list[EventV1] = []

    for zone in zones:
        state = get_or_create_state(session, pos.site_id, pos.asset_id, zone.zone_id)
        # Skip late / out-of-order fixes for live evaluation (still stored).
        # Normalise last_ts: SQLite returns naive, Postgres returns aware.
        if state.last_ts is not None:
            last_ts = state.last_ts if state.last_ts.tzinfo else state.last_ts.replace(tzinfo=UTC)
            if pos.ts <= last_ts:
                continue

        cfg = parse_rules(zone.kind, zone.rules)
        inside_raw = point_in_polygon(pos.lat, pos.lon, zone.geometry)
        dist_out = 0.0 if inside_raw else distance_to_polygon_m(pos.lat, pos.lon, zone.geometry)

        transition = engine.step(
            state, inside_raw=inside_raw, dist_outside_m=dist_out, ts=pos.ts, cfg=cfg
        )
        if transition == "entry":
            ev = on_entry(pos=pos, zone=zone, cfg=cfg, asset_class=asset_class)
            if ev is not None:
                events.append(ev)
        if state.inside:
            events.extend(on_inzone_fix(pos=pos, zone=zone, cfg=cfg, state=state))

        state.last_ts = pos.ts

    for ev in events:
        persist_event(session, ev)
    return events
