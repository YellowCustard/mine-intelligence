"""Exception layer — "what needs attention right now", quiet when healthy.

The dashboard's job is to surface the few things a supervisor must act on, not to
make them scan a healthy fleet for problems. This computes those exceptions from
state that already exists, each with a drill-down reference (asset / event /
incident) so the UI can jump straight to the evidence.

Every group is derived from concrete state, never a guess. Critically, a stopped
machine and an offline tracker are **separate** groups: a comms outage is a
data-quality problem, not machine downtime (brief / PR-A). When every group is
empty the fleet is healthy and the layer says so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.operations import equipment
from minemonitor.storage.models import Asset, Event, Incident
from minemonitor.storage.repositories import latest_positions

# Incident states that still need a human.
_OPEN_INCIDENT_STATES = ("open", "acknowledged", "investigating", "assigned")


def compute_exceptions(
    session: Session, site_id: str, now: datetime, offline_after_s: float
) -> dict[str, Any]:
    """The current exception groups for a site. ``healthy`` is true iff all empty."""
    # Unacknowledged critical alarms — the loudest thing in the queue.
    critical_alarms = [
        {
            "event_id": e.event_id,
            "ts": e.ts,
            "type": e.type,
            "asset_id": e.asset_id,
            "zone_id": e.zone_id,
            "summary": e.summary,
            "state": e.state,
        }
        for e in session.execute(
            select(Event)
            .where(
                Event.site_id == site_id,
                Event.severity == "critical",
                Event.state != "resolved",
            )
            .order_by(Event.ts.desc())
            .limit(100)
        ).scalars()
    ]

    # Incidents still needing a human; unassigned ones flagged.
    unresolved_incidents = [
        {
            "incident_id": i.incident_id,
            "severity": i.severity,
            "state": i.state,
            "asset_id": i.asset_id,
            "assignee": i.assignee,
            "unassigned": i.assignee is None,
            "summary": i.summary,
        }
        for i in session.execute(
            select(Incident)
            .where(Incident.site_id == site_id, Incident.state.in_(_OPEN_INCIDENT_STATES))
            .order_by(Incident.created_at.desc())
            .limit(100)
        ).scalars()
    ]

    # Derive equipment state for each asset's latest fix; split stopped vs offline.
    classes = {
        a.asset_id: a.asset_class
        for a in session.execute(select(Asset).where(Asset.site_id == site_id)).scalars()
    }
    stopped_machines: list[dict[str, Any]] = []
    offline_trackers: list[dict[str, Any]] = []
    for p in latest_positions(session, site_id):
        status = equipment.derive_state(
            latest_ts=p.ts,
            speed_kph=p.speed_kph,
            ignition=p.ignition,
            now=now,
            offline_after_s=offline_after_s,
        )
        item = {
            "asset_id": p.asset_id,
            "asset_class": classes.get(p.asset_id, "unknown"),
            "state": status.state,
            "data_age_s": status.data_age_s,
            "reason": status.reason,
        }
        if status.state == equipment.STOPPED:
            stopped_machines.append(item)
        elif status.state == equipment.OFFLINE:
            offline_trackers.append(item)

    groups: dict[str, list[dict[str, Any]]] = {
        "critical_alarms": critical_alarms,
        "unresolved_incidents": unresolved_incidents,
        "stopped_machines": stopped_machines,
        "offline_trackers": offline_trackers,
    }
    counts = {name: len(items) for name, items in groups.items()}
    return {
        "healthy": all(c == 0 for c in counts.values()),
        "counts": counts,
        "groups": groups,
    }
