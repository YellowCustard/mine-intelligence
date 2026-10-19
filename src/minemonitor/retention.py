"""Per-data-class retention: a deletion job that actually runs (brief §4).

Retention is configurable per class — raw positions, derived analytics (metrics
and haul cycles, which share one window), events, operational annotations
(incidents, delay classifications and shift handovers), gate passages, and the audit trail —
with generous defaults. A value of 0 days means "keep forever" (skip). The job is
idempotent and logs an audit entry, so deletion is accountable.

The audit trail is retained longer than the operational data it describes:
accountability for a deletion has to outlive the deleted record.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.config import get_settings
from minemonitor.storage.models import (
    AccessEvent,
    AccessQuarantine,
    AssetMetrics,
    AuditLog,
    BreachOperator,
    BreachRecord,
    DelayClassification,
    Event,
    HaulCycle,
    Incident,
    IncidentNote,
    Notification,
    Position,
    ShiftHandover,
    ShiftReportDelivery,
    VisionCameraHealth,
    VisionNodeHealth,
    VisionObservation,
    VisionOperationalEvent,
    VisionZoneRollup,
)

log = logging.getLogger("minemonitor.retention")


def run_retention(
    session: Session,
    *,
    now: datetime | None = None,
    positions_days: int,
    metrics_days: int,
    events_days: int,
    annotations_days: int = 0,
    access_days: int = 0,
    audit_days: int = 0,
    vision_observations_days: int = 0,
    vision_health_days: int = 0,
    actor: str = "system",
) -> dict[str, int]:
    """Delete data older than each class's retention window. Commits.

    ``metrics_days`` governs both metric buckets and haul cycles — one "derived
    analytics" class, both recomputable from stored positions. ``annotations_days``
    governs the operational-annotation tables (delay classifications by their end,
    shift handovers by creation, and **closed** incidents by their close time —
    open incidents are live work and are never age-deleted). ``access_days`` governs gate
    passages (``access_events``) by their time: each names a person, so it is personal data
    with its own window. ``audit_days`` prunes
    the audit trail itself (0 = keep forever), and closed breach records by their close
    time; the entry written for this run is created after the cutoff, so a run never
    deletes its own record.
    """
    now = now or datetime.now(UTC)
    deleted: dict[str, int] = {}

    def _purge(model, ts_col, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = now - timedelta(days=days)
        res = session.execute(delete(model).where(ts_col < cutoff))
        return res.rowcount or 0

    def _purge_closed_incidents(days: int) -> tuple[int, int]:
        """Delete closed incidents (and their notes) whose close time is past cutoff.

        Only *closed* incidents are eligible — an open or in-progress incident is
        live work regardless of age. Notes carry a FK to the incident, so they are
        removed first.
        """
        if days <= 0:
            return 0, 0
        cutoff = now - timedelta(days=days)
        ids = (
            session.execute(
                select(Incident.incident_id).where(
                    Incident.closed_at.is_not(None), Incident.closed_at < cutoff
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return 0, 0
        notes = session.execute(
            delete(IncidentNote).where(IncidentNote.incident_id.in_(ids))
        ).rowcount
        incs = session.execute(delete(Incident).where(Incident.incident_id.in_(ids))).rowcount
        return incs or 0, notes or 0

    def _purge_closed_breaches(days: int) -> int:
        """Delete breach records closed before the audit window (and their links).

        A breach record is part of the accountability trail, so it follows the audit
        window, counted from its close. An open breach is live work and is never deleted.
        """
        if days <= 0:
            return 0
        cutoff = now - timedelta(days=days)
        ids = (
            session.execute(
                select(BreachRecord.breach_id).where(
                    BreachRecord.closed_at.is_not(None), BreachRecord.closed_at < cutoff
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return 0
        session.execute(delete(BreachOperator).where(BreachOperator.breach_id.in_(ids)))
        res = session.execute(delete(BreachRecord).where(BreachRecord.breach_id.in_(ids)))
        return res.rowcount or 0

    def _purge_events(days: int) -> tuple[int, int]:
        """Delete alarms past cutoff, with their notification rows.

        ``notifications`` and ``incidents`` carry a foreign key to the alarm, so a plain
        delete would fail and roll back the whole run. A notification is the alarm's
        delivery record and goes with it. An alarm an incident still points at is kept:
        the incident is live work or annotation history. Closed incidents are purged
        before this, so the run that deletes an incident deletes its old alarm too.
        """
        if days <= 0:
            return 0, 0
        cutoff = now - timedelta(days=days)
        due = (Event.ts < cutoff) & ~exists().where(Incident.event_id == Event.event_id)
        notes = session.execute(
            delete(Notification).where(Notification.event_id.in_(select(Event.event_id).where(due)))
        ).rowcount
        events = session.execute(delete(Event).where(due)).rowcount
        return events or 0, notes or 0

    deleted["positions"] = _purge(Position, Position.ts, positions_days)
    deleted["asset_metrics"] = _purge(AssetMetrics, AssetMetrics.bucket_start, metrics_days)
    deleted["haul_cycles"] = _purge(HaulCycle, HaulCycle.end_ts, metrics_days)
    # Incidents first: an alarm is deletable only once no retained incident points at it.
    deleted["incidents"], deleted["incident_notes"] = _purge_closed_incidents(annotations_days)
    deleted["events"], deleted["notifications"] = _purge_events(events_days)
    # Quarantined gate events are the evidence behind a clock-skew alarm: they follow events.
    deleted["access_quarantine"] = _purge(
        AccessQuarantine, AccessQuarantine.first_seen_at, events_days
    )
    # A queued shift report holds a recipient's address and the shift's figures: it is a
    # record of something sent, like an alarm, and follows the events class.
    deleted["shift_report_deliveries"] = _purge(
        ShiftReportDelivery, ShiftReportDelivery.shift_end, events_days
    )
    # Gate passages name a person (operator_ref): personal data, in their own class.
    deleted["access_events"] = _purge(AccessEvent, AccessEvent.ts, access_days)
    deleted["delay_classifications"] = _purge(
        DelayClassification, DelayClassification.end_ts, annotations_days
    )
    deleted["shift_handovers"] = _purge(ShiftHandover, ShiftHandover.created_at, annotations_days)
    # Vision classes: raw observations + health are their own classes; derived operational
    # events follow the events class and zone rollups the derived-analytics class.
    deleted["vision_observations"] = _purge(
        VisionObservation, VisionObservation.ts, vision_observations_days
    )
    deleted["vision_node_health"] = _purge(
        VisionNodeHealth, VisionNodeHealth.ts, vision_health_days
    )
    deleted["vision_camera_health"] = _purge(
        VisionCameraHealth, VisionCameraHealth.ts, vision_health_days
    )
    deleted["vision_operational_events"] = _purge(
        VisionOperationalEvent, VisionOperationalEvent.ts, events_days
    )
    deleted["vision_zone_rollups"] = _purge(VisionZoneRollup, VisionZoneRollup.ts, metrics_days)
    deleted["breach_records"] = _purge_closed_breaches(audit_days)
    deleted["audit_log"] = _purge(AuditLog, AuditLog.ts, audit_days)

    audit.record(
        session,
        actor=actor,
        action="retention.run",
        entity_type="site",
        entity_id=None,
        site_id=None,
        detail={
            "deleted": deleted,
            "policy_days": {
                "positions": positions_days,
                "metrics": metrics_days,
                "events": events_days,
                "annotations": annotations_days,
                "access": access_days,
                "audit": audit_days,
                "vision_observations": vision_observations_days,
                "vision_health": vision_health_days,
            },
        },
    )
    session.commit()
    log.info("retention complete", extra={"deleted": deleted})
    return deleted


def run_from_config(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Run retention using the configured per-class windows."""
    s = get_settings()
    return run_retention(
        session,
        now=now,
        positions_days=s.retain_positions_days,
        metrics_days=s.retain_metrics_days,
        events_days=s.retain_events_days,
        annotations_days=s.retain_annotations_days,
        access_days=s.retain_access_days,
        audit_days=s.retain_audit_days,
        vision_observations_days=s.retain_vision_observations_days,
        vision_health_days=s.retain_vision_health_days,
    )
