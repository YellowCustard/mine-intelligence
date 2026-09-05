"""HTTP ingest for raw telemetry.

The device sends a position *without* ``received_at``; the server stamps it at
the ingest boundary (brief §6). Validation and storage go through the shared
ingest service so HTTP and MQTT behave identically.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from minemonitor.ingest.service import PositionIngest, store_position
from minemonitor.storage.db import get_db
from minemonitor.storage.repositories import list_positions

router = APIRouter(tags=["ingest"])
log = logging.getLogger("minemonitor.ingest")


@router.post("/ingest/positions", status_code=202)
def ingest_position(payload: PositionIngest, db: Session = Depends(get_db)) -> dict[str, object]:
    """Validate, stamp and idempotently store a single position."""
    created = store_position(db, payload)
    log.info(
        "position ingested",
        extra={
            "site_id": payload.site_id,
            "asset_id": payload.asset_id,
            "source": "http",
            "position_created": created,
        },
    )
    return {"stored": True, "created": created}


@router.get("/sites/{site_id}/positions")
def read_positions(
    site_id: str,
    asset_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Read stored positions for a site (always site-scoped), newest first."""
    rows = list_positions(db, site_id=site_id, asset_id=asset_id, limit=limit)
    return [
        {
            "schema": "asset.position.v1",
            "site_id": r.site_id,
            "asset_id": r.asset_id,
            "ts": r.ts,
            "received_at": r.received_at,
            "lat": r.lat,
            "lon": r.lon,
            "altitude_m": r.altitude_m,
            "speed_kph": r.speed_kph,
            "heading_deg": r.heading_deg,
            "hdop": r.hdop,
            "satellites": r.satellites,
            "ignition": r.ignition,
            "source": r.source,
        }
        for r in rows
    ]
