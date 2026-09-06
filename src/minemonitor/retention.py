"""Per-data-class retention: a deletion job that actually runs (brief §4).

Retention is configurable per class — raw positions, derived analytics (metrics
and haul cycles, which share one window), events, and the audit trail — with
generous defaults. A value of 0 days means "keep forever" (skip). The job is
idempotent and logs an audit entry, so deletion is accountable.

The audit trail is retained longer than the operational data it describes:
accountability for a deletion has to outlive the deleted record.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.config import get_settings
from minemonitor.storage.models import AssetMetrics, AuditLog, Event, HaulCycle, Position

log = logging.getLogger("minemonitor.retention")


def run_retention(
    session: Session,
    *,
    now: datetime | None = None,
    positions_days: int,
    metrics_days: int,
    events_days: int,
    audit_days: int = 0,
    actor: str = "system",
) -> dict[str, int]:
    """Delete data older than each class's retention window. Commits.

    ``metrics_days`` governs both metric buckets and haul cycles — one "derived
    analytics" class, both recomputable from stored positions. ``audit_days``
    prunes the audit trail itself (0 = keep forever); the entry written for this
    run is created after the cutoff, so a run never deletes its own record.
    """
    now = now or datetime.now(UTC)
    deleted: dict[str, int] = {}

    def _purge(model, ts_col, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = now - timedelta(days=days)
        res = session.execute(delete(model).where(ts_col < cutoff))
        return res.rowcount or 0

    deleted["positions"] = _purge(Position, Position.ts, positions_days)
    deleted["asset_metrics"] = _purge(AssetMetrics, AssetMetrics.bucket_start, metrics_days)
    deleted["haul_cycles"] = _purge(HaulCycle, HaulCycle.end_ts, metrics_days)
    deleted["events"] = _purge(Event, Event.ts, events_days)
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
        audit_days=s.retain_audit_days,
    )
