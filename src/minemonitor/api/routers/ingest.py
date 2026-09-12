"""HTTP ingest for raw telemetry.

The device sends a position *without* ``received_at``; the server stamps it at
the ingest boundary (brief §6). Validation and storage go through the shared
ingest service so HTTP and MQTT behave identically.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from minemonitor.auth.deps import require_device, require_viewer
from minemonitor.ingest.service import PositionIngest, store_and_process
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User
from minemonitor.storage.repositories import list_positions

router = APIRouter(tags=["ingest"])
log = logging.getLogger("minemonitor.ingest")


@router.post("/ingest/positions", status_code=202)
def ingest_position(
    payload: PositionIngest,
    db: Session = Depends(get_db),
    device: User = Depends(require_device),
) -> dict[str, object]:
    """Validate, stamp, store, and run zone/rule processing for a position.

    Object-level authorisation (brief §6/§10): the ``site_id`` and ``asset_id``
    ride in the device payload, so a site-scoped device credential must not be
    able to publish telemetry for another site merely by changing the body. A
    site-scoped device may write only its own site; a global (site-less) device
    account — the shared ingestor/simulator — may still write any site.
    """
    if device.site_id is not None and payload.site_id != device.site_id:
        log.warning(
            "rejected cross-site ingest",
            extra={
                "device": device.username,
                "device_site": device.site_id,
                "payload_site": payload.site_id,
                "asset_id": payload.asset_id,
            },
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "device not authorised to publish for this site"
        )
    created, events = store_and_process(db, payload)
    log.info(
        "position ingested",
        extra={
            "site_id": payload.site_id,
            "asset_id": payload.asset_id,
            "source": "http",
            "position_created": created,
            "events": len(events),
        },
    )
    return {"stored": True, "created": created, "events": len(events)}


@router.get("/sites/{site_id}/positions", dependencies=[Depends(require_viewer)])
def read_positions(
    site_id: str,
    asset_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Read stored positions for a site (always site-scoped).

    ``since``/``until`` bound the device-time window and ``order`` (``asc``/``desc``)
    picks the direction — together they drive historical playback of a track over a
    shift or around an event. Raw telemetry is immutable; this is a pure read.
    """
    rows = list_positions(
        db, site_id=site_id, asset_id=asset_id, limit=limit, since=since, until=until, order=order
    )
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
