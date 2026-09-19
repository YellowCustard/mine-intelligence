"""Reconciliation domain service (FP-09 groundwork). Callers commit.

Records measured material quantities (write-once, idempotent per ``measurement_id``), mirroring
the weighbridge measured-record discipline. This is ingestion only — it computes **no**
expectation, variance or theft conclusion (those are deferred slices, gated on weightometer
feeds + baselined historical variation). Every stored value is measured, with its stage, unit
and source provenance retained for the later, reproducible reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts.reconciliation import MaterialStage
from minemonitor.storage.models import MaterialMeasurement

_VALID_STAGES: frozenset[str] = frozenset(
    ("ore", "crusher", "mill", "tanks", "carbon", "furnace", "output", "gate")
)


def measurement_id(site_id: str, stage: str, source: str, source_ref: str) -> str:
    """Deterministic id, so re-importing the same measurement never double-counts production."""
    return f"mm-{site_id}-{stage}-{source}-{source_ref}"


def record_measurement(
    session: Session,
    *,
    site_id: str,
    stage: str,
    ts: datetime,
    quantity: float,
    unit: str,
    source_ref: str,
    created_by: str,
    now: datetime | None = None,
    source: str = "manual",
    assay_ref: str | None = None,
    quality: str | None = None,
) -> tuple[MaterialMeasurement, bool]:
    """Record one measured material quantity. Returns (row, created). No commit is forced here.

    Idempotent on the deterministic id derived from (site, stage, source, source_ref): a
    re-import returns the stored row untouched with ``created=False``. Rejects an unknown stage
    or a negative quantity loudly (never coerced) — malformed input cannot corrupt totals.
    """
    if stage not in _VALID_STAGES:
        raise ValueError(f"stage must be one of {sorted(_VALID_STAGES)}")
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    now = now or datetime.now(UTC)
    mid = measurement_id(site_id, stage, source, source_ref)
    existing = session.get(MaterialMeasurement, mid)
    if existing is not None:
        return existing, False
    row = MaterialMeasurement(
        id=mid,
        site_id=site_id,
        stage=stage,
        ts=ts,
        quantity=quantity,
        unit=unit,
        assay_ref=assay_ref,
        quality=quality,
        source=source,
        created_at=now,
        created_by=created_by,
    )
    session.add(row)
    return row, True


def list_measurements(
    session: Session,
    site_id: str,
    *,
    stage: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[MaterialMeasurement]:
    """List measurements for a site (always site-scoped), newest first."""
    stmt = select(MaterialMeasurement).where(MaterialMeasurement.site_id == site_id)
    if stage is not None:
        stmt = stmt.where(MaterialMeasurement.stage == stage)
    if since is not None:
        stmt = stmt.where(MaterialMeasurement.ts >= since)
    if until is not None:
        stmt = stmt.where(MaterialMeasurement.ts < until)
    stmt = stmt.order_by(MaterialMeasurement.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())


# Re-exported for callers that want the typed stage literal without importing the contract.
__all__ = ["MaterialStage", "list_measurements", "measurement_id", "record_measurement"]
