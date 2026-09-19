"""Laboratory API (FP-08) — ingest assay results, correct them (append-only), read history.

Mine Monitor ingests instrument output **directly** so a result is never retyped. The measured
result is stored write-once and hashed (tamper-evidence); corrections never mutate it — they are
append-only annotations carrying the actor and reason, audited. A value outside its element's
configured bounds raises an advisory ``lab_anomaly`` alarm for review — the platform flags, it
never alters a measured record (brief §15). ``sample_ref`` is an opaque lab label, never personal
data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.config import get_settings
from minemonitor.ingest.adapters.spectraa import (
    ingest_lab_results,
    normalise_lab_result,
    parse_spectraa_csv,
)
from minemonitor.laboratory import service
from minemonitor.storage.db import get_db
from minemonitor.storage.models import LaboratoryCorrection, LaboratoryResult, User

router = APIRouter(tags=["laboratory"])


class LabResultIn(BaseModel):
    """A single assay result submitted directly (manual entry or an API push)."""

    model_config = ConfigDict(extra="forbid")

    instrument: str = Field(min_length=1)
    sample_ref: str = Field(min_length=1)
    element: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    time: str = Field(min_length=1)  # ISO-8601 with offset
    method: str | None = None
    batch_ref: str | None = None
    source: str = "manual"
    original_file_ref: str | None = None


class LabCsvIn(BaseModel):
    """A SpectrAA CSV export ingested wholesale — the 'no manual retyping' path."""

    model_config = ConfigDict(extra="forbid")

    instrument: str = Field(min_length=1)
    csv: str = Field(min_length=1)


class LabCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrected_value: float
    reason: str = Field(min_length=1)
    unit: str | None = None


def _result_dict(r: LaboratoryResult) -> dict[str, Any]:
    return {
        "result_id": r.id,
        "site_id": r.site_id,
        "instrument": r.instrument,
        "sample_ref": r.sample_ref,
        "ts": r.ts,
        "element": r.element,
        "value": r.value,
        "unit": r.unit,
        "method": r.method,
        "batch_ref": r.batch_ref,
        "original_hash": r.original_hash,
        "original_file_ref": r.original_file_ref,
        "source": r.source,
        "provenance": "measured",
    }


def _correction_dict(c: LaboratoryCorrection) -> dict[str, Any]:
    return {
        "correction_id": c.id,
        "result_id": c.result_id,
        "corrected_value": c.corrected_value,
        "unit": c.unit,
        "actor": c.actor,
        "reason": c.reason,
        "ts": c.ts,
        "provenance": "corrected",
    }


def _bounds() -> dict[str, tuple[float, float]]:
    return service.parse_bounds(get_settings().lab_anomaly_bounds)


@router.post("/sites/{site_id}/laboratory/results", status_code=201)
def ingest_result(
    site_id: str,
    body: LabResultIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Ingest one assay result (supervisor; audited). Idempotent per (sample, element, time).

    The submitted record is stored write-once and hashed; a value outside its element's bounds
    raises an advisory ``lab_anomaly`` alarm. Returns the stored result and whether an alarm fired.
    """
    try:
        kwargs = normalise_lab_result(body.model_dump())
        row, created, alarms = service.ingest_result(
            db, site_id=site_id, created_by=user.username, bounds=_bounds(), **kwargs
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="laboratory.result.ingest",
        entity_type="laboratory_result",
        entity_id=row.id,
        site_id=site_id,
        detail={"element": row.element, "created": created, "alarms": len(alarms)},
    )
    db.commit()
    return {"result": _result_dict(row), "created": created, "alarms_raised": len(alarms)}


@router.post("/sites/{site_id}/laboratory/results/csv", status_code=201)
def ingest_csv(
    site_id: str,
    body: LabCsvIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Ingest a SpectrAA CSV export (supervisor; audited). Idempotent; malformed rows reject."""
    try:
        raw_records = parse_spectraa_csv(body.csv, instrument=body.instrument)
        new, alarms = ingest_lab_results(
            db,
            site_id,
            raw_records,
            created_by=user.username,
            bounds=_bounds(),
            commit=False,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="laboratory.result.ingest_csv",
        entity_type="laboratory_result",
        entity_id=None,
        site_id=site_id,
        detail={"received": len(raw_records), "new": len(new), "alarms": len(alarms)},
    )
    db.commit()
    return {"received": len(raw_records), "created": len(new), "alarms_raised": len(alarms)}


@router.get("/sites/{site_id}/laboratory/results")
def list_results(
    site_id: str,
    sample_ref: str | None = None,
    element: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """List assay results for a site (viewer; newest first). Site-scoped."""
    rows = service.list_results(
        db, site_id, sample_ref=sample_ref, element=element, since=since, until=until, limit=limit
    )
    return [_result_dict(r) for r in rows]


@router.get("/sites/{site_id}/laboratory/results/{result_id}")
def get_result(
    site_id: str,
    result_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """One result with its correction history and effective (latest) value (viewer)."""
    row = db.get(LaboratoryResult, result_id)
    if row is None or row.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "laboratory result not found")
    corrections = service.list_corrections(db, site_id, result_id)
    effective = service.effective_value(db, site_id, result_id)
    return {
        "result": _result_dict(row),
        "corrections": [_correction_dict(c) for c in corrections],
        "effective_value": (
            {"value": effective[0], "basis": effective[1]} if effective is not None else None
        ),
    }


@router.post("/sites/{site_id}/laboratory/results/{result_id}/corrections", status_code=201)
def add_correction(
    site_id: str,
    result_id: str,
    body: LabCorrectionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Append a correction to a result (supervisor; audited). Never mutates the original."""
    row = service.add_correction(
        db,
        site_id=site_id,
        result_id_=result_id,
        corrected_value=body.corrected_value,
        actor=user.username,
        reason=body.reason,
        now=datetime.now(UTC),
        unit=body.unit,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "laboratory result not found")
    audit.record(
        db,
        actor=user.username,
        action="laboratory.result.correct",
        entity_type="laboratory_result",
        entity_id=result_id,
        site_id=site_id,
        detail={"corrected_value": body.corrected_value, "reason": body.reason},
    )
    db.commit()
    return {"correction": _correction_dict(row)}
