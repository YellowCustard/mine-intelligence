"""Per-data-class retention: a deletion job that actually runs (brief §4).

Retention is configurable per class — raw positions, derived analytics (metrics
and haul cycles, which share one window), events, operational annotations
(incidents, delay classifications and shift handovers), and the audit trail —
with generous defaults. A value of 0 days means "keep forever" (skip). The job is
idempotent and logs an audit entry, so deletion is accountable.

The audit trail is retained longer than the operational data it describes:
accountability for a deletion has to outlive the deleted record.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.config import get_settings
from minemonitor.storage.models import (
    AssetMetrics,
    AuditLog,
    DelayClassification,
    Event,
    HaulCycle,
    Incident,
    IncidentNote,
    Position,
    ShiftHandover,
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
    audit_days: int = 0,
    actor: str = "system",
) -> dict[str, int]:
    """Delete data older than each class's retention window. Commits.

    ``metrics_days`` governs both metric buckets and haul cycles — one "derived
    analytics" class, both recomputable from stored positions. ``annotations_days``
    governs the operational-annotation tables (delay classifications by their end,
    shift handovers by creation, and **closed** incidents by their close time —
    open incidents are live work and are never age-deleted). ``audit_days`` prunes
    the audit trail itself (0 = keep forever); the entry written for this run is
    created after the cutoff, so a run never deletes its own record.
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

    deleted["positions"] = _purge(Position, Position.ts, positions_days)
    deleted["asset_metrics"] = _purge(AssetMetrics, AssetMetrics.bucket_start, metrics_days)
    deleted["haul_cycles"] = _purge(HaulCycle, HaulCycle.end_ts, metrics_days)
    deleted["events"] = _purge(Event, Event.ts, events_days)
    deleted["delay_classifications"] = _purge(
        DelayClassification, DelayClassification.end_ts, annotations_days
    )
    deleted["shift_handovers"] = _purge(ShiftHandover, ShiftHandover.created_at, annotations_days)
    deleted["incidents"], deleted["incident_notes"] = _purge_closed_incidents(annotations_days)
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
                "audit": audit_days,
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
        audit_days=s.retain_audit_days,
    )
