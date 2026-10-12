"""MQTT acknowledgement: a message is acked only once its outcome is final (audit M4).

With paho's automatic acks, PUBACK went out whenever the callback returned, including
when a graceful stop during a database outage ended the store-retry loop: the broker
then discarded a fix that was never stored. Acks are now manual and sent only after
the fix is committed, or after a message is rejected for good.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as paho
import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.ingest import mqtt as mqtt_mod
from minemonitor.ingest.mqtt import MqttIngestor
from minemonitor.storage.models import Position

_TOPIC = "mm/kn-zw-01/HT-102/position"


class _Msg:
    def __init__(self, topic: str, payload: dict[str, object], mid: int = 7) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()
        self.mid = mid
        self.qos = 1


def _body(ts: str = "2026-09-05T11:00:00Z") -> dict[str, object]:
    return {
        "schema": "asset.position.v1",
        "site_id": "kn-zw-01",
        "asset_id": "HT-102",
        "ts": ts,
        "lat": -17.8252,
        "lon": 31.0335,
        "speed_kph": 5.0,
        "ignition": True,
    }


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments the ingestor built its paho client with."""
    seen: dict[str, Any] = {}
    real = paho.Client

    def spy(*args: Any, **kwargs: Any) -> paho.Client:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mqtt_mod.mqtt, "Client", spy)
    return seen


@pytest.fixture
def ingestor(
    factory: sessionmaker[Session], built: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> MqttIngestor:
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    ing = MqttIngestor(write_retry_s=0.01)
    ing.prefix = "mm"
    return ing


def _record_acks(ing: MqttIngestor, factory: sessionmaker[Session]) -> list[tuple[int, int, int]]:
    """Each ack as (mid, qos, positions stored at that moment)."""
    acks: list[tuple[int, int, int]] = []

    def ack(mid: int, qos: int) -> paho.MQTTErrorCode:
        with factory() as s:
            stored = s.execute(select(func.count()).select_from(Position)).scalar_one()
        acks.append((mid, qos, stored))
        return paho.MQTTErrorCode.MQTT_ERR_SUCCESS

    ing._client.ack = ack  # type: ignore[method-assign]
    return acks


def test_the_client_acks_manually(ingestor: MqttIngestor, built: dict[str, Any]) -> None:
    assert built.get("manual_ack") is True


def test_a_stored_fix_is_acked_after_it_is_committed(
    ingestor: MqttIngestor, factory: sessionmaker[Session]
) -> None:
    acks = _record_acks(ingestor, factory)
    ingestor._on_message(None, None, _Msg(_TOPIC, _body(), mid=7))
    assert acks == [(7, 1, 1)]  # one ack, sent with the fix already in the database


def test_a_message_rejected_for_good_is_acked(
    ingestor: MqttIngestor, factory: sessionmaker[Session]
) -> None:
    acks = _record_acks(ingestor, factory)
    ingestor._on_message(None, None, _Msg(_TOPIC, {"schema": "nonsense"}, mid=8))
    assert [a[:2] for a in acks] == [(8, 1)]  # it will never become good: do not redeliver


def test_a_fix_not_stored_when_the_ingestor_stops_is_not_acked(
    ingestor: MqttIngestor, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    acks = _record_acks(ingestor, factory)

    def db_down(*_a: object, **_k: object) -> object:
        raise sa_exc.OperationalError("INSERT", {}, Exception("server closed connection"))

    monkeypatch.setattr(mqtt_mod, "store_and_process", db_down)
    t = threading.Thread(target=ingestor._on_message, args=(None, None, _Msg(_TOPIC, _body())))
    t.start()
    t.join(0.3)
    assert t.is_alive()  # retrying while the database is down
    ingestor._stop.set()  # graceful stop mid-outage
    t.join(5)
    assert not t.is_alive()
    assert acks == []  # the broker keeps it for the persistent session's next connect


def test_a_vision_record_not_stored_when_the_ingestor_stops_is_not_acked(
    ingestor: MqttIngestor, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from minemonitor.vision import service as vision

    acks = _record_acks(ingestor, factory)
    monkeypatch.setattr(
        vision, "parse", lambda raw: SimpleNamespace(site_id="kn-zw-01", camera_id="cam-1")
    )
    monkeypatch.setattr(mqtt_mod, "vision_topic_matches_payload", lambda *a: True)

    def db_down(*_a: object, **_k: object) -> dict[str, object]:
        raise sa_exc.OperationalError("INSERT", {}, Exception("server closed connection"))

    monkeypatch.setattr(vision, "ingest", db_down)
    msg = _Msg("mm/kn-zw-01/vision/cam-1/observation", {"schema": "x"})
    t = threading.Thread(target=ingestor._on_message, args=(None, None, msg))
    t.start()
    t.join(0.3)
    assert t.is_alive()
    ingestor._stop.set()
    t.join(5)
    assert not t.is_alive()
    assert acks == []
