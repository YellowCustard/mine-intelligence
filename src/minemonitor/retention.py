"""Per-data-class retention: a deletion job that actually runs (brief §4).

Retention is configurable per class — raw positions, derived analytics (metrics
and haul cycles, which share one window), events, operational annotations
(incidents, delay classifications and shift handovers), gate passages, operational
records (weigh tickets, fuel, laboratory, material, dispatch and maintenance records),
equipment readings (generator telemetry), vision data, and the audit trail —
with generous defaults. A value of 0 days means "keep forever" (skip). The job is
idempotent and logs an audit entry, so deletion is accountable.

The audit trail is retained longer than the operational data it describes:
accountability for a deletion has to outlive the deleted record.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session, aliased

from minemonitor import audit
from minemonitor.config import get_settings
from minemonitor.dispatch.service import TERMINAL_JOB_STATUSES
from minemonitor.storage.models import (
    AccessEvent,
    AccessQuarantine,
    AssetMetrics,
    AuditLog,
    BreachOperator,
    BreachRecord,
    DelayClassification,
    DispatchAssignment,
    DispatchJob,
    Event,
    FuelTankReading,
    FuelTransaction,
    GeneratorReading,
    HaulCycle,
    Incident,
    IncidentNote,
    LaboratoryCorrection,
    LaboratoryResult,
    MaterialMeasurement,
    Notification,
    Position,
    ShiftHandover,
    ShiftReportDelivery,
    VisionCameraHealth,
    VisionNodeHealth,
    VisionObservation,
    VisionOperationalEvent,
    VisionVendorEvent,
    VisionZoneRollup,
    WeighTicket,
    WorkOrder,
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
    records_days: int = 0,
    readings_days: int = 0,
    actor: str = "system",
) -> dict[str, int]:
    """Delete data older than each class's retention window. Commits.

    ``metrics_days`` governs both metric buckets and haul cycles — one "derived
    analytics" class, both recomputable from stored positions. ``annotations_days``
    governs the operational-annotation tables (delay classifications by their end,
    shift handovers by creation, and **closed** incidents by their close time —
    open incidents are live work and are never age-deleted). ``access_days`` governs gate
    passages (``access_events``) by their time: each names a person, so it is personal data
    with its own window. ``records_days`` governs the operational records (weigh tickets,
    fuel transactions and tank dips, laboratory results, material measurements, ended
    dispatch jobs and closed work orders); ``readings_days`` the generator readings, keeping
    each generator's latest. ``audit_days`` prunes
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

    def _purge_lab_results(days: int) -> tuple[int, int]:
        """Delete laboratory results measured before cutoff, with their corrections.

        A correction carries a FK to its result and means nothing without it, so it goes with
        the result whatever its own age; corrections are removed first.
        """
        if days <= 0:
            return 0, 0
        due = LaboratoryResult.ts < now - timedelta(days=days)
        fixes = session.execute(
            delete(LaboratoryCorrection).where(
                LaboratoryCorrection.result_id.in_(select(LaboratoryResult.id).where(due))
            )
        ).rowcount
        results = session.execute(delete(LaboratoryResult).where(due)).rowcount
        return results or 0, fixes or 0

    def _purge_dispatch(days: int) -> tuple[int, int]:
        """Delete ended dispatch jobs created before cutoff, with their assignments.

        Only a complete or cancelled job is eligible: an open job is live work regardless of
        age. Assignments carry a FK to the job, so they are removed first.
        """
        if days <= 0:
            return 0, 0
        due = DispatchJob.status.in_(TERMINAL_JOB_STATUSES) & (
            DispatchJob.created_at < now - timedelta(days=days)
        )
        assignments = session.execute(
            delete(DispatchAssignment).where(
                DispatchAssignment.job_id.in_(select(DispatchJob.id).where(due))
            )
        ).rowcount
        jobs = session.execute(delete(DispatchJob).where(due)).rowcount
        return jobs or 0, assignments or 0

    def _purge_work_orders(days: int) -> int:
        """Delete work orders closed before cutoff, keeping each component's latest service.

        An open or in-progress work order has no close time and is never age-deleted. The
        latest completed service of an asset component is the baseline the maintenance health
        indicator measures from, so it is kept until a newer service replaces it.
        """
        if days <= 0:
            return 0
        newer = aliased(WorkOrder)
        latest_service = (
            (WorkOrder.type == "service")
            & (WorkOrder.status == "done")
            & ~exists().where(
                newer.site_id == WorkOrder.site_id,
                newer.asset_id == WorkOrder.asset_id,
                newer.component == WorkOrder.component,
                newer.type == "service",
                newer.status == "done",
                newer.closed_at > WorkOrder.closed_at,
            )
        )
        res = session.execute(
            delete(WorkOrder).where(
                WorkOrder.closed_at.is_not(None),
                WorkOrder.closed_at < now - timedelta(days=days),
                ~latest_service,
            )
        )
        return res.rowcount or 0

    def _purge_generator_readings(days: int) -> int:
        """Delete generator readings before cutoff, keeping each generator's latest.

        The latest reading carries the engine hours the maintenance health indicator reads; a
        generator that has gone quiet keeps its last known figure rather than none.
        """
        if days <= 0:
            return 0
        newer = aliased(GeneratorReading)
        res = session.execute(
            delete(GeneratorReading).where(
                GeneratorReading.ts < now - timedelta(days=days),
                exists().where(
                    newer.site_id == GeneratorReading.site_id,
                    newer.asset_id == GeneratorReading.asset_id,
                    newer.ts > GeneratorReading.ts,
                ),
            )
        )
        return res.rowcount or 0

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
    # Vendor NVR events are the basis of an alarm, like first-party operational events.
    deleted["vision_vendor_events"] = _purge(VisionVendorEvent, VisionVendorEvent.ts, events_days)
    # Operational records back production and gold accounting: one long window.
    deleted["weigh_tickets"] = _purge(WeighTicket, WeighTicket.ts, records_days)
    deleted["fuel_transactions"] = _purge(FuelTransaction, FuelTransaction.ts, records_days)
    deleted["fuel_tank_readings"] = _purge(FuelTankReading, FuelTankReading.ts, records_days)
    deleted["laboratory_results"], deleted["laboratory_corrections"] = _purge_lab_results(
        records_days
    )
    deleted["material_measurements"] = _purge(
        MaterialMeasurement, MaterialMeasurement.ts, records_days
    )
    deleted["dispatch_jobs"], deleted["dispatch_assignments"] = _purge_dispatch(records_days)
    deleted["work_orders"] = _purge_work_orders(records_days)
    # Equipment readings are high-volume telemetry, like raw positions.
    deleted["generator_readings"] = _purge_generator_readings(readings_days)
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
                "records": records_days,
                "readings": readings_days,
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
        records_days=s.retain_records_days,
        readings_days=s.retain_readings_days,
    )
