"""Zone CRUD. Rules travel in the ``rules`` payload — adding one is a data change."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor.storage.db import get_db
from minemonitor.zones.repository import (
    delete_zone,
    get_zone,
    list_zones,
    upsert_zone,
)

router = APIRouter(tags=["zones"])

ZoneKind = Literal["loading", "unloading", "restricted", "speed_limited", "generic"]


class ZoneIn(BaseModel):
    """Request body for creating/replacing a zone."""

    zone_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ZoneKind
    geometry: dict[str, Any]  # GeoJSON Polygon (lon, lat rings)
    rules: dict[str, Any] = Field(default_factory=dict)


def _to_dict(row: Any) -> dict[str, Any]:
    return {
        "zone_id": row.zone_id,
        "site_id": row.site_id,
        "name": row.name,
        "kind": row.kind,
        "geometry": row.geometry,
        "rules": row.rules,
    }


@router.get("/sites/{site_id}/zones")
def get_zones(site_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_to_dict(z) for z in list_zones(db, site_id)]


@router.get("/sites/{site_id}/zones/{zone_id}")
def read_zone(site_id: str, zone_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_zone(db, site_id, zone_id)
    if row is None:
        raise HTTPException(status_code=404, detail="zone not found")
    return _to_dict(row)


@router.put("/sites/{site_id}/zones/{zone_id}", status_code=200)
def put_zone(
    site_id: str, zone_id: str, body: ZoneIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    if body.zone_id != zone_id:
        raise HTTPException(status_code=400, detail="zone_id in path and body differ")
    row = upsert_zone(
        db,
        site_id=site_id,
        zone_id=zone_id,
        name=body.name,
        kind=body.kind,
        geometry=body.geometry,
        rules=body.rules,
    )
    db.commit()
    return _to_dict(row)


@router.post("/sites/{site_id}/zones", status_code=201)
def create_zone(site_id: str, body: ZoneIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = upsert_zone(
        db,
        site_id=site_id,
        zone_id=body.zone_id,
        name=body.name,
        kind=body.kind,
        geometry=body.geometry,
        rules=body.rules,
    )
    db.commit()
    return _to_dict(row)


@router.delete("/sites/{site_id}/zones/{zone_id}", status_code=204)
def remove_zone(site_id: str, zone_id: str, db: Session = Depends(get_db)) -> None:
    if not delete_zone(db, site_id, zone_id):
        raise HTTPException(status_code=404, detail="zone not found")
    db.commit()
