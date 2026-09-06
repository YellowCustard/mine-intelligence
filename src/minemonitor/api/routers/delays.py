"""Downtime / delay classification API — annotations over lost time.

Classifications are stored separately from telemetry and never mutate positions,
so derived analytics stay reproducible. Reads are viewer-level; creating or
correcting a classification requires supervisor and is audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.operations import delays
from minemonitor.storage.db import get_db
from minemonitor.storage.models import DelayClassification, User

router = APIRouter(tags=["delays"])


class DelayIn(BaseModel):
    category: str
    start_ts: datetime
    end_ts: datetime
    asset_id: str | None = None
    zone_id: str | None = None
    note: str | None = Field(default=None)


def _dict(row: DelayClassification) -> dict[str, Any]:
    return {
        "id": row.id,
        "site_id": row.site_id,
        "asset_id": row.asset_id,
        "zone_id": row.zone_id,
        "category": row.category,
        "start_ts": row.start_ts,
        "end_ts": row.end_ts,
        "note": row.note,
        "source": row.source,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


@router.get("/delay-categories", dependencies=[Depends(require_viewer)])
def delay_categories() -> list[str]:
    """The known delay categories, in display order (for the UI dropdown)."""
    return list(delays.DELAY_CATEGORIES)


@router.get("/sites/{site_id}/delays", dependencies=[Depends(require_viewer)])
def get_delays(
    site_id: str,
    category: str | None = Query(default=None),
    asset_id: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List delay classifications for a site, newest first."""
    rows = delays.list_classifications(
        db, site_id, category=category, asset_id=asset_id, start=start, end=end, limit=limit
    )
    return [_dict(r) for r in rows]


@router.post("/sites/{site_id}/delays", status_code=201)
def create_delay(
    site_id: str,
    body: DelayIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Classify a period of lost time. Validated and audited."""
    try:
        row = delays.create_classification(
            db,
            site_id=site_id,
            category=body.category,
            start_ts=body.start_ts,
            end_ts=body.end_ts,
            actor=user.username,
            asset_id=body.asset_id,
            zone_id=body.zone_id,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="delay.classify",
        entity_type="delay_classification",
        entity_id=row.id,
        site_id=site_id,
        detail={"category": body.category},
    )
    db.commit()
    return _dict(row)


@router.delete("/sites/{site_id}/delays/{delay_id}", status_code=204)
def delete_delay(
    site_id: str,
    delay_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> None:
    """Remove a mis-classification. Audited (the annotation, not telemetry, is removed)."""
    row = delays.get_classification(db, site_id, delay_id)
    if row is None:
        raise HTTPException(status_code=404, detail="delay classification not found")
    db.delete(row)
    audit.record(
        db,
        actor=user.username,
        action="delay.delete",
        entity_type="delay_classification",
        entity_id=delay_id,
        site_id=site_id,
    )
    db.commit()
