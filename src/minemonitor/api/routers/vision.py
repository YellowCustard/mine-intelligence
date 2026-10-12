"""First-party vision API (Stage B): edge ingest, edge config, rule registry, reads.

- ``POST /ingest/vision`` — a **device** principal (the edge node) submits one record or a batch
  of ``vision.*.v1`` contracts. The whole batch is validated before anything is stored and is
  rejected loudly (422, with the failing index) if any record is malformed, names an unregistered
  camera, or cites an unknown rule version — never partially stored, never coerced. A site-scoped
  device may only publish for its own site. Replays are idempotent.
- ``GET .../vision/config`` — what the edge needs to run offline (scene zones, zone policy,
  active rules). Device or admin.
- Rule versions are immutable, admin-only and audited; reads are viewer.

Advisory only. No endpoint here accepts or returns identity, faces, or frame bytes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_device, require_viewer
from minemonitor.contracts.vision import VisionRuleVersionV1
from minemonitor.devices import service as devices
from minemonitor.operations.shifts import resolve_shift, resolve_shift_by_id
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User, VisionOperationalEvent, VisionZoneRollup
from minemonitor.vision import registry, scene, service
from minemonitor.vision.service import VisionIngestError

router = APIRouter(tags=["vision"])
log = logging.getLogger("minemonitor.vision")

MAX_BATCH = 500


@router.post("/ingest/vision", status_code=202)
def ingest_vision(
    body: dict[str, Any] | list[dict[str, Any]] = Body(...),
    db: Session = Depends(get_db),
    device: User = Depends(require_device),
) -> dict[str, Any]:
    """Validate and store edge vision records (one or a batch); promote alarms."""
    items = body if isinstance(body, list) else [body]
    if not items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty batch")
    if len(items) > MAX_BATCH:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"batch exceeds {MAX_BATCH} records"
        )
    parsed = []
    for i, raw in enumerate(items):
        try:
            rec = service.parse(raw)
        except VisionIngestError as exc:
            log.error("rejected vision record", extra={"index": i, "error": str(exc)})
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, {"index": i, "error": str(exc)}
            ) from exc
        site_id = rec.site_id
        if device.site_id is not None and site_id != device.site_id:
            log.warning(
                "rejected cross-site vision ingest",
                extra={
                    "device": device.username,
                    "device_site": device.site_id,
                    "payload_site": site_id,
                },
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "device not authorised to publish for this site"
            )
        refusal = devices.vision_refusal(db, device, site_id)
        if refusal is not None:
            log.warning(
                "rejected device vision ingest",
                extra={"device": device.username, "site_id": site_id, "reason": refusal},
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, refusal)
        parsed.append(rec)
    received_at = datetime.now(UTC)
    results = []
    try:
        for i, rec in enumerate(parsed):
            try:
                results.append(service.ingest(db, rec, received_at=received_at))
            except VisionIngestError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, {"index": i, "error": str(exc)}
                ) from exc
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    created = sum(1 for r in results if r["created"])
    alarms = [r["alarm_event_id"] for r in results if r["created"] and r["alarm_event_id"]]
    log.info(
        "vision ingested",
        extra={
            "site_id": parsed[0].site_id,
            "records": len(results),
            "records_created": created,
            "alarms": len(alarms),
        },
    )
    return {
        "accepted": len(results),
        "created": created,
        "duplicates": len(results) - created,
        "alarms": alarms,
    }


@router.get("/sites/{site_id}/vision/config")
def vision_config(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_device)
) -> dict[str, Any]:
    """Edge configuration: scene zones per camera, zone policy, active rule versions."""
    return service.edge_config(db, site_id)


# -- rule registry -----------------------------------------------------------------------------


@router.get("/sites/{site_id}/vision/rules", dependencies=[Depends(require_viewer)])
def list_rules(
    site_id: str,
    active: bool = Query(default=False, description="Only versions active now"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = service.list_rule_versions(db, site_id, active_at=datetime.now(UTC) if active else None)
    return [service.rule_to_contract(r) for r in rows]


@router.post("/sites/{site_id}/vision/rules", status_code=201)
def publish_rule(
    site_id: str,
    rule: VisionRuleVersionV1,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Publish an immutable rule version (admin; audited). An existing version is 409."""
    if rule.site_id != site_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rule site_id does not match the path")
    try:
        row = service.create_rule_version(db, rule, author=user.username)
    except VisionIngestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.rule.publish",
        entity_type="vision_rule_version",
        entity_id=row.id,
        site_id=site_id,
        detail={
            "rule_id": row.rule_id,
            "rule_version": row.rule_version,
            "event_type": row.event_type,
            "parameters": row.parameters,
        },
    )
    db.commit()
    return service.rule_to_contract(row)


@router.post("/sites/{site_id}/vision/rules/defaults", status_code=201)
def install_defaults(
    site_id: str, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> dict[str, Any]:
    """Install the default person-first rule set at 1.0.0 (admin; audited; idempotent)."""
    created = service.install_default_rules(db, site_id, author=user.username)
    audit.record(
        db,
        actor=user.username,
        action="vision.rule.install_defaults",
        entity_type="vision_rule_version",
        site_id=site_id,
        detail={"installed": [r.id for r in created]},
    )
    db.commit()
    return {"installed": [service.rule_to_contract(r) for r in created]}


@router.post("/sites/{site_id}/vision/rules/{rule_id}/{rule_version}/retire")
def retire_rule(
    site_id: str,
    rule_id: str,
    rule_version: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Retire a rule version (sets ``active_to``; admin; audited). Past events stay valid."""
    try:
        row = service.retire_rule_version(db, site_id, rule_id, rule_version)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.rule.retire",
        entity_type="vision_rule_version",
        entity_id=row.id,
        site_id=site_id,
    )
    db.commit()
    return service.rule_to_contract(row)


class RecomputeIn(BaseModel):
    """Replay one camera's stored observations through one rule version."""

    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(min_length=1)
    since: AwareDatetime
    until: AwareDatetime
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)


@router.post("/sites/{site_id}/vision/recompute")
def recompute(
    site_id: str,
    body: RecomputeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Recompute operational events from stored observations (admin; audited; no live alarms).

    Events under the given rule version are stored beside earlier ones — history is never
    rewritten, so a rule improvement can be compared against what the old rule concluded.
    """
    if body.until < body.since:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "until precedes since")
    try:
        result = service.recompute(
            db,
            site_id,
            camera_id=body.camera_id,
            since=body.since,
            until=body.until,
            rule_id=body.rule_id,
            rule_version=body.rule_version,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except VisionIngestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.recompute",
        entity_type="vision_rule_version",
        entity_id=service.rule_row_id(site_id, body.rule_id, body.rule_version),
        site_id=site_id,
        detail={
            "camera_id": body.camera_id,
            "since": body.since.isoformat(),
            "until": body.until.isoformat(),
            "observations": result["observations"],
            "created": len(result["created"]),
        },
    )
    db.commit()
    return result


# -- reads ---------------------------------------------------------------------------------------


def _op_dict(r: VisionOperationalEvent) -> dict[str, Any]:
    return {
        "event_id": r.event_id,
        "site_id": r.site_id,
        "node_id": r.node_id,
        "camera_id": r.camera_id,
        "ts": r.ts,
        "received_at": r.received_at,
        "type": r.type,
        "provenance": r.provenance,
        "confidence": r.confidence,
        "rule_id": r.rule_id,
        "rule_version": r.rule_version,
        "zone_id": r.zone_id,
        "zone_version": r.zone_version,
        "subject": r.subject,
        "evidence": r.evidence,
        "alarm_event_id": r.promoted_event_id,
        "advisory": True,
    }


def _rollup_dict(r: VisionZoneRollup) -> dict[str, Any]:
    return {
        "rollup_id": r.rollup_id,
        "camera_id": r.camera_id,
        "zone_id": r.zone_id,
        "ts": r.ts,
        "bucket_s": r.bucket_s,
        "object_class": r.object_class,
        "count_max": r.count_max,
        "count_mean": r.count_mean,
        "entries": r.entries,
        "exits": r.exits,
        "dwell_s_max": r.dwell_s_max,
        "dwell_s_p50": r.dwell_s_p50,
        "observed_s": r.observed_s,
        "provenance": "observed",
    }


@router.get("/sites/{site_id}/vision/events", dependencies=[Depends(require_viewer)])
def list_vision_events(
    site_id: str,
    camera_id: str | None = Query(default=None),
    type: str | None = Query(default=None),  # noqa: A002 - query parameter name
    since: AwareDatetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = service.list_operational_events(
        db, site_id, camera_id=camera_id, type_=type, since=since, limit=limit
    )
    return [_op_dict(r) for r in rows]


@router.get("/sites/{site_id}/vision/rollups", dependencies=[Depends(require_viewer)])
def list_vision_rollups(
    site_id: str,
    camera_id: str | None = Query(default=None),
    zone_id: str | None = Query(default=None),
    since: AwareDatetime | None = Query(default=None),
    until: AwareDatetime | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10_000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = service.list_rollups(
        db, site_id, camera_id=camera_id, zone_id=zone_id, since=since, until=until, limit=limit
    )
    return [_rollup_dict(r) for r in rows]


@router.get("/sites/{site_id}/vision/health", dependencies=[Depends(require_viewer)])
def vision_health(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return service.latest_health(db, site_id)


# -- model registry (global; docs/VISION_ARCHITECTURE.md §6) ------------------------------------


class ModelIn(BaseModel):
    """Register a model version (starts at ``experimental``)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1)
    task: str = Field(default="detect", min_length=1)
    licence: str = Field(min_length=1)
    weights_sha256: str = Field(min_length=64, max_length=64)
    classes: list[str] = Field(min_length=1)
    architecture: str | None = None
    weights_ref: str | None = None
    source_url: str | None = None
    training_dataset: str | None = None
    input_size: list[int] | None = None
    notes: str | None = None


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


@router.get("/vision/models", dependencies=[Depends(require_viewer)])
def list_models(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [registry.model_dict(r) for r in registry.list_models(db)]


@router.post("/vision/models", status_code=201)
def register_model(
    body: ModelIn, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> dict[str, Any]:
    """Register a model version (admin; audited). Forbidden licences are refused outright."""
    try:
        row = registry.register_model(db, **body.model_dump())
    except registry.RegistryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.model.register",
        entity_type="vision_model",
        entity_id=row.id,
        detail={"licence": row.licence, "sha256": row.weights_sha256},
    )
    db.commit()
    return registry.model_dict(row)


@router.post("/vision/models/{model_id}/{version}/status")
def set_model_status(
    model_id: str,
    version: str,
    body: StatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Move a model through its lifecycle (admin; audited). Production needs a cleared licence."""
    try:
        row = registry.set_status(db, model_id, version, body.status)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except registry.RegistryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.model.status",
        entity_type="vision_model",
        entity_id=row.id,
        detail={"status": row.status},
    )
    db.commit()
    return registry.model_dict(row)


@router.post("/vision/models/{model_id}/{version}/metrics")
def record_model_metrics(
    model_id: str,
    version: str,
    metrics: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Record measured metrics (e.g. a bench run per hardware profile). Admin; audited."""
    try:
        row = registry.record_metrics(db, model_id, version, metrics)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.model.metrics",
        entity_type="vision_model",
        entity_id=row.id,
        detail={"keys": sorted(metrics)},
    )
    db.commit()
    return registry.model_dict(row)


# -- scene context + reporting (V4) ----------------------------------------------------------


class SceneIn(BaseModel):
    """A camera's scene: image-zones (normalised polygons → zones) and optional homography.

    Omit ``homography`` to leave calibration unchanged; send ``null`` to clear it.
    """

    model_config = ConfigDict(extra="forbid")

    image_zones: list[dict[str, Any]]
    homography: dict[str, Any] | None = None


@router.get("/sites/{site_id}/cameras/{camera_id}/scene", dependencies=[Depends(require_viewer)])
def get_scene(site_id: str, camera_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from minemonitor.storage.models import Camera

    cam = db.get(Camera, camera_id)
    if cam is None or cam.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "camera not found")
    return scene.scene_config(cam)


@router.put("/sites/{site_id}/cameras/{camera_id}/scene")
def put_scene(
    site_id: str,
    camera_id: str,
    body: SceneIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Configure a camera's image-zones and calibration (admin; audited).

    Every zone must exist for the site; polygons are normalised ``[0, 1]`` image coordinates. A
    homography marks the camera calibrated (ground points become available to the edge); without
    one the edge reports ``basis: unknown`` and no distance is ever claimed.
    """
    try:
        cam = scene.set_scene(
            db,
            site_id,
            camera_id,
            image_zones=body.image_zones,
            homography=body.homography,
            update_homography="homography" in body.model_fields_set,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except scene.SceneError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="vision.scene.update",
        entity_type="camera",
        entity_id=camera_id,
        site_id=site_id,
        detail={
            "zones": [z["zone_id"] for z in cam.image_zones or []],
            "calibration_status": cam.calibration_status,
        },
    )
    db.commit()
    return scene.scene_config(cam)


@router.get("/sites/{site_id}/vision/scene", dependencies=[Depends(require_viewer)])
def vision_scene(site_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """What each configured camera sees now: per-zone people counts, condition, open alarms."""
    return scene.scene_state(db, site_id)


def _report_window(
    db: Session,
    site_id: str,
    shift_id: str | None,
    at: datetime | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime, datetime, str | None]:
    if since is not None or until is not None:
        if since is None or until is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "give both since and until")
        if until <= since:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "until must be after since")
        return since, until, None
    window = (
        resolve_shift_by_id(db, site_id, shift_id)
        if shift_id
        else resolve_shift(db, site_id, at or datetime.now(UTC))
    )
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no shift matches")
    return window.start, window.end, window.shift_id


@router.get("/sites/{site_id}/vision/report", dependencies=[Depends(require_viewer)])
def vision_report(
    site_id: str,
    shift_id: str | None = Query(default=None),
    at: AwareDatetime | None = Query(default=None),
    since: AwareDatetime | None = Query(default=None),
    until: AwareDatetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """People per camera × zone for a shift (default: the current shift) or explicit window."""
    start, end, sid = _report_window(db, site_id, shift_id, at, since, until)
    return {**scene.vision_report(db, site_id, start, end), "shift_id": sid}


@router.get(
    "/sites/{site_id}/vision/report.csv",
    dependencies=[Depends(require_viewer)],
    response_class=PlainTextResponse,
)
def vision_report_csv(
    site_id: str,
    shift_id: str | None = Query(default=None),
    at: AwareDatetime | None = Query(default=None),
    since: AwareDatetime | None = Query(default=None),
    until: AwareDatetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    start, end, sid = _report_window(db, site_id, shift_id, at, since, until)
    report = scene.vision_report(db, site_id, start, end)
    name = (sid or f"{start:%Y%m%dT%H%M}-{end:%Y%m%dT%H%M}").replace(":", "_")
    return PlainTextResponse(
        scene.report_to_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="vision-report-{name}.csv"'},
    )
