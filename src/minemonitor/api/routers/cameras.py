"""Camera estate & AI-readiness API (Mine Monitor Vision, FP-01 / RAN Mines Slice 1).

Reads are viewer-level; creating, updating, and recording assessments are admin-level and
audited (camera config is reference data, like devices/zones). Every write is site-scoped.
This slice is registry/config only — no video, no inference, no events. ``stream_url`` is a
secret: it is accepted on write but never returned (responses expose only ``has_stream_url``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.cameras import service
from minemonitor.storage.db import get_db
from minemonitor.storage.models import Camera, User

router = APIRouter(tags=["cameras"])


class CameraIn(BaseModel):
    name: str = Field(min_length=1)
    location_description: str | None = None
    make_model: str | None = None
    stream_url: str | None = None
    stream_type: str = "unknown"
    stream_kind: str = "unknown"
    resolution: str | None = None
    fps: int | None = Field(default=None, ge=0)
    codec: str | None = None
    lighting: str = "unknown"
    has_usable_ai_stream: bool | None = None
    ai_suitability: str = "unknown"
    blind_spot_notes: str | None = None
    zone_id: str | None = None
    homography: dict[str, Any] | None = None
    calibration_status: str = "none"
    model_deployed: str | None = None
    model_version: str | None = None
    health_state: str = "unknown"
    enabled: bool = True
    note: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    location_description: str | None = None
    make_model: str | None = None
    stream_url: str | None = None
    stream_type: str | None = None
    stream_kind: str | None = None
    resolution: str | None = None
    fps: int | None = Field(default=None, ge=0)
    codec: str | None = None
    lighting: str | None = None
    has_usable_ai_stream: bool | None = None
    ai_suitability: str | None = None
    blind_spot_notes: str | None = None
    zone_id: str | None = None
    homography: dict[str, Any] | None = None
    calibration_status: str | None = None
    model_deployed: str | None = None
    model_version: str | None = None
    health_state: str | None = None
    enabled: bool | None = None
    note: str | None = None


class AssessmentIn(BaseModel):
    """A Phase 0 AI-readiness audit finding for a camera."""

    has_usable_ai_stream: bool | None = None
    ai_suitability: str | None = None
    resolution: str | None = None
    fps: int | None = Field(default=None, ge=0)
    codec: str | None = None
    lighting: str | None = None
    stream_kind: str | None = None
    blind_spot_notes: str | None = None


def _camera_dict(c: Camera) -> dict[str, Any]:
    return {
        "id": c.id,
        "site_id": c.site_id,
        "name": c.name,
        "location_description": c.location_description,
        "make_model": c.make_model,
        "has_stream_url": c.stream_url is not None,  # secret never returned
        "stream_type": c.stream_type,
        "stream_kind": c.stream_kind,
        "resolution": c.resolution,
        "fps": c.fps,
        "codec": c.codec,
        "lighting": c.lighting,
        "has_usable_ai_stream": c.has_usable_ai_stream,
        "ai_suitability": c.ai_suitability,
        "blind_spot_notes": c.blind_spot_notes,
        "zone_id": c.zone_id,
        "has_homography": c.homography is not None,
        "calibration_status": c.calibration_status,
        "model_deployed": c.model_deployed,
        "model_version": c.model_version,
        "health_state": c.health_state,
        "last_seen": c.last_seen,
        "enabled": c.enabled,
        "note": c.note,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.post("/sites/{site_id}/cameras", status_code=201)
def create_camera(
    site_id: str,
    body: CameraIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Register a camera (admin — reference data; audited)."""
    data = body.model_dump()
    name = data.pop("name")
    try:
        cam = service.create_camera(db, site_id=site_id, name=name, now=datetime.now(UTC), **data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=admin.username,
        action="camera.create",
        entity_type="camera",
        entity_id=cam.id,
        site_id=site_id,
        detail={"name": cam.name, "ai_suitability": cam.ai_suitability},
    )
    db.commit()
    return _camera_dict(cam)


@router.get("/sites/{site_id}/cameras")
def list_cameras(
    site_id: str,
    ai_suitability: str | None = None,
    health_state: str | None = None,
    zone_id: str | None = None,
    enabled: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [
        _camera_dict(c)
        for c in service.list_cameras(
            db,
            site_id,
            ai_suitability=ai_suitability,
            health_state=health_state,
            zone_id=zone_id,
            enabled=enabled,
        )
    ]


@router.get("/sites/{site_id}/cameras/readiness")
def readiness(
    site_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """Estate-wide AI-readiness rollup (the Phase 0 camera-audit headline)."""
    return service.readiness_summary(db, site_id)


@router.get("/sites/{site_id}/cameras/{camera_id}")
def get_camera(
    site_id: str,
    camera_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    cam = service.get_camera(db, site_id, camera_id)
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    return _camera_dict(cam)


@router.patch("/sites/{site_id}/cameras/{camera_id}")
def update_camera(
    site_id: str,
    camera_id: str,
    body: CameraUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Update a camera (admin; audited). Only supplied fields change."""
    fields = body.model_dump(exclude_unset=True)
    try:
        cam = service.update_camera(db, site_id, camera_id, now=datetime.now(UTC), **fields)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    audit.record(
        db,
        actor=admin.username,
        action="camera.update",
        entity_type="camera",
        entity_id=cam.id,
        site_id=site_id,
        detail={"fields": sorted(fields)},
    )
    db.commit()
    return _camera_dict(cam)


@router.post("/sites/{site_id}/cameras/{camera_id}/assessment")
def record_assessment(
    site_id: str,
    camera_id: str,
    body: AssessmentIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Record an AI-readiness assessment (a Phase 0 audit finding; admin; audited)."""
    fields = body.model_dump(exclude_unset=True)
    try:
        cam = service.record_assessment(db, site_id, camera_id, now=datetime.now(UTC), **fields)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    audit.record(
        db,
        actor=admin.username,
        action="camera.assessment",
        entity_type="camera",
        entity_id=cam.id,
        site_id=site_id,
        detail={
            "ai_suitability": cam.ai_suitability,
            "has_usable_ai_stream": cam.has_usable_ai_stream,
        },
    )
    db.commit()
    return _camera_dict(cam)
