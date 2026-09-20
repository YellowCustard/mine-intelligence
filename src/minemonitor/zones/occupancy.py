"""Shared zone-occupancy counting — the one place confirmed membership is computed.

Both the periodic breach rule (``rules/occupancy.py``) and the occupancy config/read API
(``api/routers/zones.py``) need "how many assets are confirmed inside this zone right now" and
"is a zone over its cap". Keeping that logic here means the count exists once and the rule and
the dashboard can never disagree.

Occupancy is **cross-asset** and **source-agnostic**: it counts post-debounce ``inside`` flags
in ``asset_zone_state`` (GNSS today, vision tracks later via the same shape). ``max_occupancy``
opts a zone in and lives in the zone's ``rules`` JSON, so setting a capacity is a data change,
never a migration.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.storage.models import AssetZoneState, Zone

_SEVERITIES = {"info", "warning", "critical"}


def parse_cap(rules: dict[str, Any] | None) -> int | None:
    """The zone's ``max_occupancy`` as a non-negative int, or None if unset/malformed.

    Tolerant and non-guessing: a missing, non-numeric, or negative value yields None (the zone
    is simply not capped) rather than raising — a malformed cap must never break the tick or the
    read.
    """
    if not rules:
        return None
    raw = rules.get("max_occupancy")
    if raw is None:
        return None
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return None
    return cap if cap >= 0 else None


def cap_severity(rules: dict[str, Any] | None) -> str:
    """The configured breach severity for a zone (default ``warning``)."""
    sev = str((rules or {}).get("severity", "warning"))
    return sev if sev in _SEVERITIES else "warning"


def current_occupancy(session: Session, site_id: str, zone_id: str) -> int:
    """Count of assets confirmed inside a zone (post-debounce). Site-scoped."""
    return int(
        session.execute(
            select(func.count())
            .select_from(AssetZoneState)
            .where(
                AssetZoneState.site_id == site_id,
                AssetZoneState.zone_id == zone_id,
                AssetZoneState.inside.is_(True),
            )
        ).scalar_one()
    )


def occupancy_status(session: Session, site_id: str) -> list[dict[str, Any]]:
    """Per-capped-zone occupancy vs capacity for a site (the dashboard/read shape).

    Only zones that opt in (a valid ``max_occupancy``) are returned; each carries the current
    confirmed ``occupancy``, the ``max_occupancy`` cap, whether it is ``over``, and the
    configured breach ``severity``.
    """
    zones = list(session.execute(select(Zone).where(Zone.site_id == site_id)).scalars().all())
    out: list[dict[str, Any]] = []
    for z in zones:
        cap = parse_cap(z.rules)
        if cap is None:
            continue
        count = current_occupancy(session, site_id, z.zone_id)
        out.append(
            {
                "zone_id": z.zone_id,
                "name": z.name,
                "max_occupancy": cap,
                "occupancy": count,
                "over": count > cap,
                "severity": cap_severity(z.rules),
            }
        )
    return out
