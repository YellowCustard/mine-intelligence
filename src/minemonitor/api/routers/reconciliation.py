"""Gold-reconciliation API (FP-09 groundwork) — ingest and read material measurements.

Ingestion only: measured quantities through the gold circuit, recorded with source provenance.
It computes **no** expectation, variance or theft conclusion (deferred slices). The platform
records the measured facts; a human concludes. Advisory posture throughout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.contracts.reconciliation import MaterialStage
from minemonitor.reconciliation import service
from minemonitor.storage.db import get_db
from minemonitor.storage.models import MaterialMeasurement, User

router = APIRouter(tags=["reconciliation"])


class MeasurementIn(BaseModel):
    """A single measured material quantity at a circuit stage."""

    model_config = ConfigDict(extra="forbid")

    stage: MaterialStage
    time: str = Field(min_length=1)  # ISO-8601 with offset
    quantity: float = Field(ge=0)
    unit: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)  # the source's own id for idempotency
    source: str = "manual"
    assay_ref: str | None = None
    quality: str | None = None


def _parse_ts(raw_time: str) -> datetime:
    try:
        ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"'time' is not ISO-8601: {raw_time!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"'time' must be timezone-aware: {raw_time!r}")
    return ts


def _measurement_dict(m: MaterialMeasurement) -> dict[str, Any]:
    return {
        "measurement_id": m.id,
        "site_id": m.site_id,
        "stage": m.stage,
        "ts": m.ts,
        "quantity": m.quantity,
        "unit": m.unit,
        "assay_ref": m.assay_ref,
        "quality": m.quality,
        "source": m.source,
        "provenance": "measured",
    }


@router.post("/sites/{site_id}/reconciliation/measurements", status_code=201)
def ingest_measurement(
    site_id: str,
    body: MeasurementIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Ingest one material measurement (supervisor; audited). Idempotent per source ref."""
    try:
        ts = _parse_ts(body.time)
        row, created = service.record_measurement(
            db,
            site_id=site_id,
            stage=body.stage,
            ts=ts,
            quantity=body.quantity,
            unit=body.unit,
            source_ref=body.source_ref,
            created_by=user.username,
            source=body.source,
            assay_ref=body.assay_ref,
            quality=body.quality,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="reconciliation.measurement.ingest",
        entity_type="material_measurement",
        entity_id=row.id,
        site_id=site_id,
        detail={"stage": row.stage, "created": created},
    )
    db.commit()
    return {"measurement": _measurement_dict(row), "created": created}


@router.get("/sites/{site_id}/reconciliation/measurements")
def list_measurements(
    site_id: str,
    stage: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """List material measurements for a site (viewer; newest first). Site-scoped."""
    rows = service.list_measurements(
        db, site_id, stage=stage, since=since, until=until, limit=limit
    )
    return [_measurement_dict(m) for m in rows]
