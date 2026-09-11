"""Device provisioning API (brief §11) — admin-only, audited.

Binds a device identity to one asset so the broker ACL and the ingest check can be
derived from it. Registration, enable/disable are audited; listing is admin read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin
from minemonitor.devices import service
from minemonitor.storage.db import get_db
from minemonitor.storage.models import Device, User

router = APIRouter(tags=["devices"])


class DeviceIn(BaseModel):
    device_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source: str | None = None
    expected_interval_s: int | None = Field(default=None, ge=1)
    note: str | None = None


def _device_dict(d: Device) -> dict[str, Any]:
    return {
        "device_id": d.device_id,
        "site_id": d.site_id,
        "asset_id": d.asset_id,
        "enabled": d.enabled,
        "source": d.source,
        "expected_interval_s": d.expected_interval_s,
        "created_at": d.created_at,
        "note": d.note,
    }


@router.get("/sites/{site_id}/devices")
def list_devices(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)
) -> list[dict[str, Any]]:
    """List provisioned devices at a site (admin only)."""
    return [_device_dict(d) for d in service.list_devices(db, site_id)]


@router.post("/sites/{site_id}/devices", status_code=201)
def register_device(
    site_id: str,
    body: DeviceIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Provision a device bound to one asset (admin only). An asset takes one device."""
    try:
        dev = service.register_device(
            db,
            device_id=body.device_id,
            site_id=site_id,
            asset_id=body.asset_id,
            source=body.source,
            expected_interval_s=body.expected_interval_s,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(
        db,
        actor=admin.username,
        action="device.register",
        entity_type="device",
        entity_id=body.device_id,
        site_id=site_id,
        detail={"asset_id": body.asset_id},
    )
    db.commit()
    return _device_dict(dev)


@router.post("/sites/{site_id}/devices/{device_id}/enabled")
def set_enabled(
    site_id: str,
    device_id: str,
    enabled: bool,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Enable or disable a device (admin only). A disabled device may not ingest."""
    dev = service.get_device(db, device_id)
    if dev is None or dev.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    service.set_enabled(db, device_id, enabled)
    audit.record(
        db,
        actor=admin.username,
        action="device.enabled",
        entity_type="device",
        entity_id=device_id,
        site_id=site_id,
        detail={"enabled": enabled},
    )
    db.commit()
    return _device_dict(dev)
