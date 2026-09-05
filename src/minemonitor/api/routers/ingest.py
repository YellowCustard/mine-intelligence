"""HTTP ingest for raw telemetry.

The device sends a position *without* ``received_at``; the server stamps it at
the ingest boundary (``received_at`` is never set by the device — brief §6). The
body is validated against the contract and rejected loudly on any malformation;
device data is never silently coerced (brief §12).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor.contracts import AssetPositionV1
from minemonitor.storage.db import get_db
from minemonitor.storage.repositories import insert_position, list_positions

router = APIRouter(tags=["ingest"])
log = logging.getLogger("minemonitor.ingest")


class PositionIngest(BaseModel):
    """Device-facing position payload. ``received_at`` is added server-side."""

    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(default="asset.position.v1", alias="schema")
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    ts: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float | None = None
    speed_kph: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    hdop: float | None = Field(default=None, ge=0)
    satellites: int | None = Field(default=None, ge=0)
    ignition: bool | None = None
    source: str | None = None


@router.post("/ingest/positions", status_code=202)
def ingest_position(payload: PositionIngest, db: Session = Depends(get_db)) -> dict[str, object]:
    """Validate, stamp and idempotently store a single position."""
    canonical = AssetPositionV1(
        **payload.model_dump(by_alias=True, exclude_none=False),
        received_at=datetime.now(UTC),
    )
    created = insert_position(db, canonical)
    log.info(
        "position ingested",
        extra={
            "site_id": canonical.site_id,
            "asset_id": canonical.asset_id,
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
