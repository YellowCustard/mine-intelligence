"""Operations surface: shift resolution and shift-definition configuration.

The shift is the primary operational unit. Definitions are per-site config
(admin-managed, audited); the current shift is derived on demand. Reads are
viewer-level; changes require admin and are audited like any other config change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import audit, heartbeat
from minemonitor.api.routers.health import _mqtt_reachable
from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.config import get_settings
from minemonitor.operations import bottlenecks as bottlenecks_mod
from minemonitor.operations import dataquality as dataquality_mod
from minemonitor.operations import exceptions as exceptions_mod
from minemonitor.operations import systemhealth as systemhealth_mod
from minemonitor.operations import trends as trends_mod
from minemonitor.operations.scorecard import scorecard_with_comparison
from minemonitor.operations.shifts import definitions, resolve_shift
from minemonitor.storage.db import get_db
from minemonitor.storage.models import ShiftDefinition, User

router = APIRouter(tags=["operations"])


class ShiftDefinitionIn(BaseModel):
    start_hour_local: int = Field(ge=0, le=23)
    duration_hours: int = Field(ge=1, le=24)
    enabled: bool = True


def _def_dict(d: ShiftDefinition) -> dict[str, Any]:
    return {
        "site_id": d.site_id,
        "name": d.name,
        "start_hour_local": d.start_hour_local,
        "duration_hours": d.duration_hours,
        "enabled": d.enabled,
    }


@router.get("/sites/{site_id}/shifts/current", dependencies=[Depends(require_viewer)])
def current_shift(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The shift instance containing 'now', or ``{"shift": null}`` if outside all shifts."""
    window = resolve_shift(db, site_id, datetime.now(UTC))
    return {"shift": window.as_dict() if window is not None else None}


@router.get("/sites/{site_id}/scorecard", dependencies=[Depends(require_viewer)])
def shift_scorecard(
    site_id: str,
    at: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Scorecard for the shift containing ``at`` (default now), vs the previous shift.

    Every figure is observed or derived from stored data — no target or benchmark.
    """
    moment = at or datetime.now(UTC)
    return scorecard_with_comparison(db, site_id, moment)


@router.get("/sites/{site_id}/exceptions", dependencies=[Depends(require_viewer)])
def exceptions(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """What needs attention now: critical alarms, open incidents, stopped machines,
    offline trackers. ``healthy`` is true when there is nothing to act on.
    """
    offline_after_s = get_settings().offline_threshold_s
    return exceptions_mod.compute_exceptions(db, site_id, datetime.now(UTC), offline_after_s)


@router.get("/sites/{site_id}/trends", dependencies=[Depends(require_viewer)])
def trends(
    site_id: str,
    shifts: int = Query(default=7, ge=1, le=60),
    at: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-shift series of the headline metrics over the last N shifts (observed only)."""
    return trends_mod.compute_trends(db, site_id, at or datetime.now(UTC), count=shifts)


@router.get("/sites/{site_id}/bottlenecks", dependencies=[Depends(require_viewer)])
def bottlenecks(
    site_id: str,
    at: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Bottleneck observations for the shift containing ``at`` — correlations, not causes."""
    window = resolve_shift(db, site_id, at or datetime.now(UTC))
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no shift matches")
    return bottlenecks_mod.compute_bottlenecks(db, site_id, window)


@router.get("/sites/{site_id}/data-quality", dependencies=[Depends(require_viewer)])
def data_quality(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Telemetry trustworthiness: stale/late/out-of-order/impossible fixes + confidence."""
    offline_after_s = get_settings().offline_threshold_s
    return dataquality_mod.compute_data_quality(db, site_id, datetime.now(UTC), offline_after_s)


@router.get("/sites/{site_id}/system-health", dependencies=[Depends(require_viewer)])
def system_health(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Platform-vs-field health: distinguishes an app failure from trackers going quiet."""
    settings = get_settings()
    ingestor_fresh = heartbeat.is_fresh(db, heartbeat.INGESTOR, stale_s=settings.heartbeat_stale_s)
    mqtt_ok = _mqtt_reachable(settings.mqtt_host, settings.mqtt_port)
    return systemhealth_mod.compute_system_health(
        db,
        site_id,
        datetime.now(UTC),
        offline_after_s=settings.offline_threshold_s,
        ingestor_fresh=ingestor_fresh,
        mqtt_ok=mqtt_ok,
    )


@router.get("/sites/{site_id}/shift-definitions", dependencies=[Depends(require_viewer)])
def list_shift_definitions(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Configured shift definitions, and whether they are site config or defaults."""
    rows = (
        db.execute(select(ShiftDefinition).where(ShiftDefinition.site_id == site_id))
        .scalars()
        .all()
    )
    if rows:
        return {"source": "configured", "definitions": [_def_dict(d) for d in rows]}
    # Fall back to the effective defaults so callers always see what is in force.
    effective = [
        {"site_id": site_id, "name": n, "start_hour_local": s, "duration_hours": d, "enabled": True}
        for (n, s, d) in definitions(db, site_id)
    ]
    return {"source": "default", "definitions": effective}


@router.put("/sites/{site_id}/shift-definitions/{name}", status_code=200)
def upsert_shift_definition(
    site_id: str,
    name: str,
    body: ShiftDefinitionIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Create or update a shift definition (admin only). Audited."""
    row = db.get(ShiftDefinition, (site_id, name))
    if row is None:
        row = ShiftDefinition(site_id=site_id, name=name)
        db.add(row)
    row.start_hour_local = body.start_hour_local
    row.duration_hours = body.duration_hours
    row.enabled = body.enabled
    audit.record(
        db,
        actor=admin.username,
        action="shift_definition.upsert",
        entity_type="shift_definition",
        entity_id=name,
        site_id=site_id,
        detail={
            "start_hour_local": body.start_hour_local,
            "duration_hours": body.duration_hours,
            "enabled": body.enabled,
        },
    )
    db.commit()
    return _def_dict(row)


@router.delete("/sites/{site_id}/shift-definitions/{name}", status_code=204)
def delete_shift_definition(
    site_id: str,
    name: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    row = db.get(ShiftDefinition, (site_id, name))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "shift definition not found")
    db.delete(row)
    audit.record(
        db,
        actor=admin.username,
        action="shift_definition.delete",
        entity_type="shift_definition",
        entity_id=name,
        site_id=site_id,
    )
    db.commit()
