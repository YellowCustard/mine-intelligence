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
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_device, require_viewer
from minemonitor.contracts.vision import VisionRuleVersionV1
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User, VisionOperationalEvent, VisionZoneRollup
from minemonitor.vision import service
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
    since: datetime | None = Query(default=None),
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
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
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
