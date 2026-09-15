"""Camera estate & AI-readiness service (Mine Monitor Vision, FP-01). Callers commit.

Manages the site camera estate and its AI-readiness metadata — the tool that captures the
RAN Mines Phase 0 camera audit and the registry every vision capability reads from. This is
reference/config data only: no video, no inference, no events. ``stream_url`` is a secret and
is never returned to callers (the API surfaces only whether one is set).

Every operation is site-scoped. Enum-valued fields are validated here (the single source of
truth) so a bad value is rejected loudly rather than silently stored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import Camera

STREAM_TYPES = {"rtsp", "onvif", "file", "unknown"}
STREAM_KINDS = {"primary", "secondary", "unknown"}
LIGHTING = {"day", "night", "mixed", "poor", "unknown"}
AI_SUITABILITY = {"suitable", "marginal", "unsuitable", "unknown"}
CALIBRATION = {"none", "pending", "calibrated"}
HEALTH = {"online", "offline", "degraded", "unknown"}

_ENUMS: dict[str, set[str]] = {
    "stream_type": STREAM_TYPES,
    "stream_kind": STREAM_KINDS,
    "lighting": LIGHTING,
    "ai_suitability": AI_SUITABILITY,
    "calibration_status": CALIBRATION,
    "health_state": HEALTH,
}

# Columns a caller may set on create/update (id, site_id, timestamps are managed here).
_SETTABLE = {
    "name",
    "location_description",
    "make_model",
    "stream_url",
    "stream_type",
    "stream_kind",
    "resolution",
    "fps",
    "codec",
    "lighting",
    "has_usable_ai_stream",
    "ai_suitability",
    "blind_spot_notes",
    "zone_id",
    "homography",
    "calibration_status",
    "model_deployed",
    "model_version",
    "health_state",
    "enabled",
    "note",
}

# The subset an AI-readiness assessment (Phase 0 audit finding) may touch.
_ASSESSMENT_FIELDS = {
    "has_usable_ai_stream",
    "ai_suitability",
    "resolution",
    "fps",
    "codec",
    "lighting",
    "blind_spot_notes",
    "stream_kind",
}


def _validate(fields: dict[str, Any]) -> None:
    for key, allowed in _ENUMS.items():
        val = fields.get(key)
        if val is not None and val not in allowed:
            raise ValueError(f"{key} must be one of {sorted(allowed)}")


def create_camera(
    session: Session, *, site_id: str, name: str, now: datetime, **fields: Any
) -> Camera:
    """Create a camera. Unknown/unset AI-readiness fields default to ``unknown`` (audit later)."""
    _validate(fields)
    cam = Camera(
        id=str(ULID()),
        site_id=site_id,
        name=name,
        created_at=now,
        updated_at=now,
        **{k: v for k, v in fields.items() if k in _SETTABLE},
    )
    session.add(cam)
    return cam


def get_camera(session: Session, site_id: str, camera_id: str) -> Camera | None:
    cam = session.get(Camera, camera_id)
    if cam is None or cam.site_id != site_id:
        return None
    return cam


def list_cameras(
    session: Session,
    site_id: str,
    *,
    ai_suitability: str | None = None,
    health_state: str | None = None,
    zone_id: str | None = None,
    enabled: bool | None = None,
) -> list[Camera]:
    stmt = select(Camera).where(Camera.site_id == site_id)
    if ai_suitability is not None:
        stmt = stmt.where(Camera.ai_suitability == ai_suitability)
    if health_state is not None:
        stmt = stmt.where(Camera.health_state == health_state)
    if zone_id is not None:
        stmt = stmt.where(Camera.zone_id == zone_id)
    if enabled is not None:
        stmt = stmt.where(Camera.enabled == enabled)
    stmt = stmt.order_by(Camera.name, Camera.id)
    return list(session.execute(stmt).scalars().all())


def update_camera(
    session: Session, site_id: str, camera_id: str, *, now: datetime, **fields: Any
) -> Camera | None:
    """Partial update. Only keys present in ``fields`` (and settable) are changed."""
    cam = get_camera(session, site_id, camera_id)
    if cam is None:
        return None
    _validate(fields)
    for key, val in fields.items():
        if key in _SETTABLE:
            setattr(cam, key, val)
    cam.updated_at = now
    return cam


def record_assessment(
    session: Session, site_id: str, camera_id: str, *, now: datetime, **fields: Any
) -> Camera | None:
    """Record an AI-readiness assessment (a Phase 0 audit finding) for a camera.

    Restricted to the assessment fields; a bad key is ignored rather than mutating
    configuration the audit should not touch.
    """
    assessment = {k: v for k, v in fields.items() if k in _ASSESSMENT_FIELDS}
    return update_camera(session, site_id, camera_id, now=now, **assessment)


def readiness_summary(session: Session, site_id: str) -> dict[str, Any]:
    """Estate-wide AI-readiness rollup — the headline of the Phase 0 camera audit."""
    cams = list_cameras(session, site_id)
    by_suitability: dict[str, int] = {}
    by_health: dict[str, int] = {}
    ai_usable = 0
    for c in cams:
        by_suitability[c.ai_suitability] = by_suitability.get(c.ai_suitability, 0) + 1
        by_health[c.health_state] = by_health.get(c.health_state, 0) + 1
        if c.has_usable_ai_stream:
            ai_usable += 1
    return {
        "total": len(cams),
        "ai_usable_stream": ai_usable,
        "by_ai_suitability": by_suitability,
        "by_health_state": by_health,
    }
