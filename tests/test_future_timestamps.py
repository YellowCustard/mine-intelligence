"""A bad device clock must not silence live zone rules.

Regression: live evaluation skips any fix at or before the asset's per-zone watermark
(``asset_zone_state.last_ts``). One fix dated a year ahead moved that watermark into the
future, after which every genuine fix was skipped as out-of-order — a light vehicle
could drive into the explosives magazine with no alarm. Future-dated fixes are now
rejected at ingest, and an already-poisoned watermark is ignored.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.contracts.position import AssetPositionV1
from minemonitor.ingest.mqtt import MqttIngestor
from minemonitor.ingest.service import FutureTimestampError, PositionIngest, reject_if_future
from minemonitor.pipeline import process_position
from minemonitor.platform.metrics import metrics
from minemonitor.storage.models import Asset, AssetZoneState, Event, Position
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone
from tests.conftest import DEVICE, make_client

MAG_LAT, MAG_LON = -17.8240, 31.0290


@pytest.fixture
def magazine(db_session: Session) -> Session:
    db_session.add(Asset(asset_id="LV-07", site_id="kn-zw-01", asset_class="light_vehicle"))
    upsert_zone(
        db_session,
        site_id="kn-zw-01",
        zone_id="r1-explosives-magazine",
        name="R1 Explosives Magazine",
        kind="restricted",
        geometry=box_polygon(MAG_LAT, MAG_LON, 40),
        rules={"authorized_classes": [], "severity": "critical"},
    )
    db_session.commit()
    return db_session


def _body(ts: datetime, lat: float = MAG_LAT) -> dict[str, object]:
    return {
        "schema": "asset.position.v1",
        "site_id": "kn-zw-01",
        "asset_id": "LV-07",
        "ts": ts.isoformat(),
        "lat": lat,
        "lon": MAG_LON,
        "speed_kph": 10.0,
        "ignition": True,
    }


def _breaches(session: Session) -> int:
    return len(session.execute(select(Event).where(Event.type == "zone_breach")).all())


def test_reject_if_future_bounds() -> None:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    ok = PositionIngest.model_validate(_body(now + timedelta(seconds=119)))
    reject_if_future(ok, now=now)  # within the default 120 s skew
    late = PositionIngest.model_validate(_body(now - timedelta(days=3)))
    reject_if_future(late, now=now)  # backfill is legitimate
    bad = PositionIngest.model_validate(_body(now + timedelta(seconds=121)))
    with pytest.raises(FutureTimestampError):
        reject_if_future(bad, now=now)


def test_http_rejects_future_fix_and_breach_still_fires(magazine: Session) -> None:
    client = make_client(magazine, DEVICE)
    before = metrics().get("ingest.rejected.future_ts")
    now = datetime.now(UTC)
    r = client.post("/ingest/positions", json=_body(now + timedelta(days=365), lat=MAG_LAT + 0.01))
    assert r.status_code == 422 and "ahead of server time" in r.text
    assert metrics().get("ingest.rejected.future_ts") == before + 1
    assert magazine.execute(select(Position)).first() is None
    for i in range(3):  # genuine fixes inside the magazine
        r = client.post("/ingest/positions", json=_body(now + timedelta(seconds=i)))
        assert r.status_code == 202
    assert _breaches(magazine) == 1


class _Msg:
    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()


def test_mqtt_rejects_future_fix(magazine: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = sessionmaker(bind=magazine.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    ing = MqttIngestor()
    ing.prefix = "mm"
    topic = "mm/kn-zw-01/LV-07/position"
    now = datetime.now(UTC)
    ing._on_message(None, None, _Msg(topic, _body(now + timedelta(days=365), MAG_LAT + 0.01)))
    assert ing.rejected_count == 1 and ing.stored_count == 0
    for i in range(3):
        ing._on_message(None, None, _Msg(topic, _body(now + timedelta(seconds=i))))
    assert ing.stored_count == 3 and _breaches(magazine) == 1


def test_poisoned_watermark_is_ignored(magazine: Session) -> None:
    """A future watermark left by data stored before this fix no longer mutes the rules."""
    now = datetime.now(UTC)
    magazine.add(
        AssetZoneState(
            site_id="kn-zw-01",
            asset_id="LV-07",
            zone_id="r1-explosives-magazine",
            last_ts=now + timedelta(days=365),
        )
    )
    magazine.commit()
    for i in range(3):
        ts = now + timedelta(seconds=i)
        pos = AssetPositionV1.model_validate({**_body(ts), "received_at": ts.isoformat()})
        process_position(magazine, pos, created=True)
        magazine.commit()
    assert _breaches(magazine) == 1
