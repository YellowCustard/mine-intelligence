"""System-health centre — is the *platform* healthy, and if not, whose fault?

The existing ``/health`` probe answers "is the service up?" for a load balancer.
This answers the operational question a control room actually asks: are we still
receiving data, and if not, is it the platform (ingestor/broker/DB down — our
problem) or the field (trackers offline — a site/comms problem)? Conflating the
two sends the wrong person to fix it, so the verdict names which.

Pure read; the app-plane booleans (broker, ingestor heartbeat) are supplied by the
caller, which already checks them for ``/health``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.storage.models import Asset, Position


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def compute_system_health(
    session: Session,
    site_id: str,
    now: datetime,
    *,
    offline_after_s: float,
    ingestor_fresh: bool,
    mqtt_ok: bool,
    db_ok: bool = True,
) -> dict[str, Any]:
    """Compose the platform-vs-field health verdict for a site."""
    # Most recent fix across the site, and how many assets are currently silent.
    latest_ts = session.execute(
        select(func.max(Position.ts)).where(Position.site_id == site_id)
    ).scalar_one_or_none()
    latest_age_s = (now - _aware(latest_ts)).total_seconds() if latest_ts is not None else None

    asset_ids = [
        a.asset_id for a in session.execute(select(Asset).where(Asset.site_id == site_id)).scalars()
    ]
    silent = 0
    for asset_id in asset_ids:
        a_latest = session.execute(
            select(func.max(Position.ts)).where(
                Position.site_id == site_id, Position.asset_id == asset_id
            )
        ).scalar_one_or_none()
        if a_latest is None or (now - _aware(a_latest)).total_seconds() > offline_after_s:
            silent += 1

    app_ok = db_ok and mqtt_ok and ingestor_fresh
    # Ingest is flowing if a recent fix landed. If not, blame the app plane when it
    # is unhealthy, otherwise the field (trackers/comms) — never both at once.
    ingest_flowing = latest_age_s is not None and latest_age_s <= offline_after_s
    if app_ok and ingest_flowing:
        verdict = "healthy"
    elif not app_ok:
        verdict = "platform_degraded"  # our problem: ingestor/broker/DB
    else:
        verdict = "field_degraded"  # app is fine; trackers/comms are the issue

    return {
        "site_id": site_id,
        "verdict": verdict,
        "app_plane": {
            "db": "ok" if db_ok else "unavailable",
            "mqtt": "ok" if mqtt_ok else "unavailable",
            "ingestor": "ok" if ingestor_fresh else "stale",
        },
        "field_plane": {
            "assets": len(asset_ids),
            "silent_feeds": silent,
            "latest_fix_age_s": latest_age_s,
            "ingest_flowing": ingest_flowing,
        },
    }
