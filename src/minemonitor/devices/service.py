"""Device provisioning service (brief §11). Callers commit."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from minemonitor.devices.acl import VISION_EDGE_SOURCE
from minemonitor.devices.broker_config import mosquitto_password_hash
from minemonitor.storage.models import Device, User


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


def resolve_imei(session: Session, imei: str) -> Device | None:
    """Resolve a Teltonika IMEI to its provisioned, enabled device (device_id == IMEI).

    Returns None for an unknown or disabled IMEI. Used by the raw-TCP Teltonika
    listener to route a real tracker to its bound ``(site_id, asset_id)`` and to
    refuse an unprovisioned or decommissioned tracker at the handshake — the same
    device model that governs the MQTT path (brief §5/§11).
    """
    dev = session.get(Device, imei)
    return dev if dev is not None and dev.enabled else None


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


def rotate_secret(session: Session, device_id: str) -> str:
    """Issue a fresh broker password for a device; return the cleartext once.

    Only the Mosquitto ``$7$`` hash is stored — the cleartext is returned to the
    caller for one-time display and never persisted. The broker password file must
    be regenerated and reloaded afterwards for the new secret to take effect.
    """
    dev = session.get(Device, device_id)
    if dev is None:
        raise ValueError(f"device {device_id!r} not found")
    secret = secrets.token_urlsafe(24)
    dev.broker_pw_hash = mosquitto_password_hash(secret)
    return secret


def authorized_asset(session: Session, site_id: str, asset_id: str) -> bool:
    """True if an *enabled* tracker is provisioned for this ``(site, asset)``.

    A vision edge node is a device but not a tracker: it can never authorise positions.
    """
    row = session.execute(
        select(Device.device_id).where(
            Device.site_id == site_id,
            Device.asset_id == asset_id,
            Device.enabled.is_(True),
            or_(Device.source.is_(None), Device.source != VISION_EDGE_SOURCE),
        )
    ).first()
    return row is not None


def position_refusal(
    session: Session, principal: User, site_id: str, asset_id: str, *, strict: bool
) -> str | None:
    """Why HTTP ingest must refuse this position from ``principal``, or None (audit M8).

    A device account whose username is a provisioned ``device_id`` is the same identity the
    broker knows, so it is held to what the broker ACL lets that device write: its own asset's
    positions, and only while enabled. Any other device account (a gateway, the shared ingest
    account) is site-scoped by the router, and with ``strict`` may write only assets that have an
    enabled tracker, as the MQTT ingestor does. An admin passes every device check.
    """
    if principal.role != "device":
        return None
    dev = session.get(Device, principal.username)
    if dev is not None:
        if not dev.enabled:
            return "device is disabled"
        if dev.source == VISION_EDGE_SOURCE:
            return "a vision edge node cannot publish positions"
        if (dev.site_id, dev.asset_id) != (site_id, asset_id):
            return "device is bound to another asset"
        return None
    if strict and not authorized_asset(session, site_id, asset_id):
        return "no enabled tracker is provisioned for this asset"
    return None


def vision_refusal(session: Session, principal: User, site_id: str) -> str | None:
    """Why HTTP ingest must refuse a vision record from ``principal``, or None (audit M8).

    The broker ACL lets a vision edge node write its own site's vision topics and nothing
    else, and never lets a tracker write vision; HTTP holds a provisioned device to the same.
    """
    if principal.role != "device":
        return None
    dev = session.get(Device, principal.username)
    if dev is None:
        return None
    if not dev.enabled:
        return "device is disabled"
    if dev.source != VISION_EDGE_SOURCE:
        return "a tracker cannot publish vision records"
    if dev.site_id != site_id:
        return "device is provisioned at another site"
    return None
