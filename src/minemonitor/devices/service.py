"""Device provisioning service (brief §11). Callers commit."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import Device


def register_device(
    session: Session,
    *,
    device_id: str,
    site_id: str,
    asset_id: str,
    source: str | None = None,
    expected_interval_s: int | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Device:
    """Provision (or update) a device bound to ``(site_id, asset_id)``.

    An asset may have only one device: binding a *different* device to an asset that
    already has one is rejected — retire the old device first. Re-registering the
    same ``device_id`` updates it and re-enables it.
    """
    bound = session.execute(
        select(Device).where(Device.site_id == site_id, Device.asset_id == asset_id)
    ).scalar_one_or_none()
    if bound is not None and bound.device_id != device_id:
        raise ValueError(
            f"asset {asset_id} at {site_id} is already bound to device {bound.device_id!r}"
        )
    dev = session.get(Device, device_id)
    if dev is None:
        dev = Device(device_id=device_id, created_at=now or datetime.now(UTC))
        session.add(dev)
    dev.site_id = site_id
    dev.asset_id = asset_id
    dev.source = source
    dev.expected_interval_s = expected_interval_s
    dev.note = note
    dev.enabled = True
    return dev


def get_device(session: Session, device_id: str) -> Device | None:
    return session.get(Device, device_id)


def list_devices(session: Session, site_id: str) -> list[Device]:
    rows = session.execute(
        select(Device).where(Device.site_id == site_id).order_by(Device.device_id)
    )
    return list(rows.scalars().all())


def set_enabled(session: Session, device_id: str, enabled: bool) -> Device | None:
    dev = session.get(Device, device_id)
    if dev is not None:
        dev.enabled = enabled
    return dev


def authorized_asset(session: Session, site_id: str, asset_id: str) -> bool:
    """True if an *enabled* device is provisioned for this ``(site, asset)``."""
    row = session.execute(
        select(Device.device_id).where(
            Device.site_id == site_id,
            Device.asset_id == asset_id,
            Device.enabled.is_(True),
        )
    ).first()
    return row is not None
