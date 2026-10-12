"""Generator monitoring API (FP-11) — ingest DSE readings, list them, read run-hours health.

Readings are measured DSE controller data. Recording derives advisory alarms (a fault, or a
run while scheduled off); health reuses the deterministic maintenance indicator over the latest
reported engine hours. **Advisory only** — the platform monitors and alerts, it never controls a
genset. A value the controller does not report is null, never fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.generator import service
from minemonitor.ingest.adapters.dse_sim import normalise_reading
from minemonitor.storage.db import get_db
from minemonitor.storage.models import GeneratorReading, User

router = APIRouter(tags=["generators"])


class ReadingIn(BaseModel):
    """A single DSE generator reading submitted directly (or via the sim/live driver)."""

    model_config = ConfigDict(extra="forbid")

    time: str = Field(min_length=1)  # ISO-8601 with offset
    running: bool
    source: str = "dse_sim"
    engine_hours: float | None = Field(default=None, ge=0)
    fuel_level_pct: float | None = Field(default=None, ge=0, le=100)
    expected_running: bool | None = None
    fault: bool = False
    fault_code: str | None = None


def _reading_dict(r: GeneratorReading) -> dict[str, Any]:
    return {
        "reading_id": r.id,
        "site_id": r.site_id,
        "asset_id": r.asset_id,
        "source": r.source,
        "ts": r.ts,
        "engine_hours": r.engine_hours,
        "running": r.running,
        "fuel_level_pct": r.fuel_level_pct,
        "expected_running": r.expected_running,
        "fault": r.fault,
        "fault_code": r.fault_code,
        "measured": True,
    }


@router.post("/sites/{site_id}/generators/{asset_id}/readings", status_code=201)
def ingest_reading(
    site_id: str,
    asset_id: str,
    body: ReadingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Ingest one generator reading (supervisor; audited). Idempotent per (asset, time).

    Records the reading and raises advisory fault / unexpected-run alarms as applicable.
    """
    try:
        kwargs = normalise_reading({"asset_id": asset_id, **body.model_dump()})
        row, created, alarms = service.record_reading(db, site_id=site_id, **kwargs)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="generator.reading.ingest",
        entity_type="generator_reading",
        entity_id=row.id,
        site_id=site_id,
        detail={"asset_id": asset_id, "created": created, "alarms": len(alarms)},
    )
    db.commit()
    return {"reading": _reading_dict(row), "created": created, "alarms_raised": len(alarms)}


@router.get("/sites/{site_id}/generators/{asset_id}/readings")
def list_readings(
    site_id: str,
    asset_id: str,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """List readings for a generator (viewer; newest first). Site-scoped."""
    rows = service.list_readings(
        db, site_id, asset_id=asset_id, since=since, until=until, limit=limit
    )
    return [_reading_dict(r) for r in rows]


@router.get("/sites/{site_id}/generators/{asset_id}/health")
def generator_health(
    site_id: str,
    asset_id: str,
    component: str = "engine",
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """Run-hours maintenance health for a generator (viewer), from its latest engine hours."""
    return service.generator_health(
        db, site_id, asset_id, component=component, now=datetime.now(UTC)
    )
