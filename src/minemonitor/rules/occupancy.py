"""Zone-occupancy breach detection — a periodic aggregate check, not a per-fix rule.

A zone whose rules carry ``max_occupancy`` raises a security-relevant alarm when *more*
than that many assets are confirmed inside it at once — the "6 people in a 5-person sector"
signal from the RAN Mines requirements meeting, the headline of the theft-*prevention*
approach (restrict and account for who is where).

This is **cross-asset**: it counts confirmed membership across all assets in a zone, so it
runs on the maintenance tick against ``asset_zone_state`` (post-debounce ``inside`` flags),
not per position. Deduped like ``asset_offline``: one open alarm per zone while the
condition persists (an operator acknowledges it). Advisory only — it warns a person.

The count is source-agnostic: any source that maintains zone membership (GNSS today, vision
tracks later via the same ``asset_zone_state`` shape) feeds it without change here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.contracts.event import Severity
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.storage.models import Event, Zone
from minemonitor.zones.occupancy import cap_severity, current_occupancy, parse_cap

SOURCE = "gnss_occupancy"


def _has_open_occupancy(session: Session, site_id: str, zone_id: str) -> bool:
    """True if an unresolved zone_occupancy alarm already exists for this zone.

    Zone-scoped (``asset_id`` is null on these events), so we cannot reuse the
    asset-keyed ``has_open_event`` helper.
    """
    stmt = (
        select(Event.event_id)
        .where(
            Event.site_id == site_id,
            Event.zone_id == zone_id,
            Event.type == "zone_occupancy",
            Event.state != "resolved",
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def detect_zone_occupancy(
    session: Session, site_id: str, *, now: datetime | None = None
) -> list[EventV1]:
    """Raise zone_occupancy events where confirmed occupancy exceeds a zone's cap. Commits.

    A zone opts in by carrying ``max_occupancy`` (a positive int) in its rules payload;
    optional ``severity`` (default ``warning``) sets the alarm severity. Zones without a
    cap are ignored, so this is a no-op until a sector is configured.
    """
    now = now or datetime.now(UTC)
    zones = list(session.execute(select(Zone).where(Zone.site_id == site_id)).scalars().all())
    events: list[EventV1] = []
    for z in zones:
        cap = parse_cap(z.rules)
        if cap is None:
            continue  # not opted in, or a malformed cap → ignore, never guess
        count = current_occupancy(session, site_id, z.zone_id)
        if count <= cap:
            continue
        if _has_open_occupancy(session, site_id, z.zone_id):
            continue
        severity = cast(Severity, cap_severity(z.rules))
        events.append(
            EventV1(
                schema="event.v1",
                event_id=new_event_id(),
                site_id=site_id,
                ts=now,
                type="zone_occupancy",
                severity=severity,
                asset_id=None,
                zone_id=z.zone_id,
                source=SOURCE,
                summary=f"{count} assets in {z.name} exceeds capacity {cap}",
                detail={"occupancy": count, "max_occupancy": cap},
                advisory=True,
                state="open",
            )
        )
    for ev in events:
        persist_event(session, ev)
    session.commit()
    return events
