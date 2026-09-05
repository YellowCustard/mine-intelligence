"""Zone storage and per-asset zone-state access. Always site-scoped."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import AssetZoneState, Zone


def list_zones(session: Session, site_id: str) -> list[Zone]:
    """All zones for a site."""
    return list(session.execute(select(Zone).where(Zone.site_id == site_id)).scalars().all())


def get_zone(session: Session, site_id: str, zone_id: str) -> Zone | None:
    row = session.get(Zone, (zone_id, site_id))
    return row if row is not None and row.site_id == site_id else row


def upsert_zone(
    session: Session,
    *,
    site_id: str,
    zone_id: str,
    name: str,
    kind: str,
    geometry: dict,
    rules: dict,
) -> Zone:
    """Create or replace a zone. Caller commits."""
    row = session.get(Zone, (zone_id, site_id))
    if row is None:
        row = Zone(zone_id=zone_id, site_id=site_id)
        session.add(row)
    row.name = name
    row.kind = kind
    row.geometry = geometry
    row.rules = rules
    return row


def delete_zone(session: Session, site_id: str, zone_id: str) -> bool:
    row = session.get(Zone, (zone_id, site_id))
    if row is None or row.site_id != site_id:
        return False
    session.delete(row)
    return True


def get_or_create_state(
    session: Session, site_id: str, asset_id: str, zone_id: str
) -> AssetZoneState:
    """Fetch the debounce state row for (asset, zone), creating it if absent."""
    row = session.get(AssetZoneState, (site_id, asset_id, zone_id))
    if row is None:
        # Initialise Python-side: ORM/DB defaults only apply at flush, but the
        # engine mutates these counters before the first flush.
        row = AssetZoneState(
            site_id=site_id,
            asset_id=asset_id,
            zone_id=zone_id,
            inside=False,
            consec_in=0,
            consec_out=0,
            overspeed_consec=0,
            overspeed_fired=False,
            dwell_fired=False,
        )
        session.add(row)
    return row
