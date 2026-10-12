"""HTTP ingest holds a device to what the broker would let it write (audit M8).

On MQTT a tracker logs in with its ``device_id`` and the broker ACL lets it write only its own
asset's position topic; a vision edge node writes only its own site's vision topics. Over HTTP the
same identity was checked for its site and nothing else, so a tracker's credential could post any
asset's positions, or vision records. A device account named after a provisioned ``device_id`` is
now held to that device's binding. Strict mode (``MM_MQTT_REQUIRE_REGISTERED_DEVICE``) applies to
HTTP device accounts as it does to the ingestor.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.auth.service import create_user
from minemonitor.cameras.service import create_camera
from minemonitor.config import get_settings
from minemonitor.devices import service as devices
from minemonitor.devices.acl import VISION_EDGE_SOURCE
from minemonitor.storage.models import Asset, Position, VisionZoneRollup, Zone
from tests.conftest import ADMIN, DEVICE, make_client

_POS: dict[str, Any] = {
    "schema": "asset.position.v1",
    "site_id": "kn-zw-01",
    "asset_id": "HT-102",
    "ts": "2026-09-05T11:42:07Z",
    "lat": -17.8252,
    "lon": 31.0335,
    "speed_kph": 20.0,
    "source": "teltonika:fmb920",
}
_PW = "testpass123"


@pytest.fixture
def site(db_session: Session) -> Session:
    """HT-102 has tracker trk-102; HT-103 has none; node-1 is a vision edge node."""
    db_session.add(Asset(asset_id="HT-103", site_id="kn-zw-01", asset_class="haul_truck"))
    devices.register_device(
        db_session, device_id="trk-102", site_id="kn-zw-01", asset_id="HT-102", source="teltonika"
    )
    devices.register_device(
        db_session,
        device_id="node-1",
        site_id="kn-zw-01",
        asset_id="node-1",
        source=VISION_EDGE_SOURCE,
    )
    for name in ("trk-102", "node-1"):
        create_user(db_session, username=name, password=_PW, role="device", site_id="kn-zw-01")
    db_session.commit()
    return db_session


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MM_MQTT_REQUIRE_REGISTERED_DEVICE", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("MM_MQTT_REQUIRE_REGISTERED_DEVICE")
    get_settings.cache_clear()


def _stored(session: Session, asset_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(Position).where(Position.asset_id == asset_id)
    ).scalar_one()


def test_a_tracker_writes_its_own_asset(site: Session) -> None:
    c = make_client(site, ("trk-102", _PW))
    assert c.post("/ingest/positions", json=_POS).status_code == 202
    assert _stored(site, "HT-102") == 1


def test_a_tracker_cannot_write_another_assets_positions(site: Session) -> None:
    c = make_client(site, ("trk-102", _PW))
    r = c.post("/ingest/positions", json={**_POS, "asset_id": "HT-103"})
    assert r.status_code == 403
    assert "another asset" in r.json()["detail"]
    assert _stored(site, "HT-103") == 0


def test_a_disabled_tracker_cannot_write(site: Session) -> None:
    devices.set_enabled(site, "trk-102", False)
    site.commit()
    c = make_client(site, ("trk-102", _PW))
    assert c.post("/ingest/positions", json=_POS).status_code == 403
    assert _stored(site, "HT-102") == 0


def test_a_vision_node_cannot_write_positions(site: Session) -> None:
    c = make_client(site, ("node-1", _PW))
    r = c.post("/ingest/positions", json={**_POS, "asset_id": "node-1"})
    assert r.status_code == 403
    assert _stored(site, "node-1") == 0


def _vision(site_id: str = "kn-zw-01", camera_id: str = "cam-1") -> dict[str, Any]:
    return {
        "schema": "vision.zone_rollup.v1",
        "rollup_id": f"vr-{camera_id}-gold-room-20260928T080000Z-person",
        "site_id": site_id,
        "node_id": "node-1",
        "camera_id": camera_id,
        "zone_id": "gold-room",
        "ts": "2026-09-28T08:00:00Z",
        "bucket_s": 300,
        "object_class": "person",
        "count_max": 3,
        "count_mean": 1.4,
        "entries": 4,
        "exits": 3,
        "dwell_s_max": 140.0,
        "dwell_s_p50": 35.0,
        "observed_s": 300.0,
        "advisory": True,
    }


def test_a_tracker_cannot_write_vision_records(site: Session) -> None:
    site.add(
        Zone(
            zone_id="gold-room",
            site_id="kn-zw-01",
            name="Gold Room",
            kind="restricted",
            geometry={},
            rules={},
        )
    )
    cam = create_camera(site, site_id="kn-zw-01", name="CAM-GR-01", now=datetime.now(UTC))
    cam.image_zones = [
        {
            "zone_id": "gold-room",
            "zone_version": "v1",
            "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
        }
    ]
    site.commit()
    c = make_client(site, ("trk-102", _PW))
    r = c.post("/ingest/vision", json=_vision(camera_id=cam.id))
    assert r.status_code == 403
    assert r.json()["detail"] == "a tracker cannot publish vision records"
    assert site.execute(select(VisionZoneRollup)).first() is None


def test_a_vision_node_cannot_write_another_sites_records(site: Session) -> None:
    """Its account has no site of its own; its device row is what binds it."""
    create_user(site, username="node-1", password=_PW, role="device", site_id=None)
    site.commit()
    c = make_client(site, ("node-1", _PW))
    r = c.post("/ingest/vision", json=_vision("other-site"))
    assert r.status_code == 403
    assert r.json()["detail"] == "device is provisioned at another site"


def test_an_account_that_is_not_a_device_keeps_its_site_scope(site: Session) -> None:
    """A gateway or the shared ingest account has no device row: site scoping only, as before."""
    c = make_client(site, DEVICE)
    assert c.post("/ingest/positions", json={**_POS, "asset_id": "HT-103"}).status_code == 202


def test_strict_mode_refuses_an_asset_with_no_tracker(site: Session, strict: None) -> None:
    c = make_client(site, DEVICE)
    r = c.post("/ingest/positions", json={**_POS, "asset_id": "HT-103"})
    assert r.status_code == 403
    assert "no enabled tracker" in r.json()["detail"]
    assert _stored(site, "HT-103") == 0
    assert c.post("/ingest/positions", json=_POS).status_code == 202  # HT-102 has trk-102


def test_strict_mode_does_not_apply_to_an_admin(site: Session, strict: None) -> None:
    """The post-deploy smoke check posts a SMOKE-TEST asset as an admin."""
    c = make_client(site, ADMIN)
    assert c.post("/ingest/positions", json={**_POS, "asset_id": "HT-103"}).status_code == 202


def test_without_strict_mode_an_unprovisioned_asset_is_accepted(site: Session) -> None:
    client: TestClient = make_client(site, DEVICE)
    assert client.post("/ingest/positions", json={**_POS, "asset_id": "HT-103"}).status_code == 202
