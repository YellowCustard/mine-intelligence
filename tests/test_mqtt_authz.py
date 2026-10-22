"""MQTT ingestor enforcement: topic/payload anti-spoof, strict device mode (§10), and a
broker that refuses the ingestor's own login."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.devices import service
from minemonitor.ingest.mqtt import MqttIngestor, MqttPublisher


class _Msg:
    """The fields of a paho ``MQTTMessage`` the ingestor reads."""

    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()
        self.mid = 1
        self.qos = 1


_BASE = {
    "schema": "asset.position.v1",
    "site_id": "kn-zw-01",
    "asset_id": "HT-102",
    "ts": "2026-09-05T11:42:07Z",
    "lat": -17.8252,
    "lon": 31.0335,
    "source": "test",
}


def _ingestor(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> MqttIngestor:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    ing = MqttIngestor()
    ing.prefix = "mm"
    return ing


def test_rejects_topic_payload_mismatch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ing = _ingestor(db_session, monkeypatch)
    # Topic says OTHER, payload claims HT-102 → spoof, rejected, nothing stored.
    ing._on_message(None, None, _Msg("mm/kn-zw-01/OTHER/position", _BASE))
    assert ing.rejected_count == 1 and ing.stored_count == 0
    # Matching topic and payload → stored.
    ing._on_message(None, None, _Msg("mm/kn-zw-01/HT-102/position", _BASE))
    assert ing.stored_count == 1


def test_strict_mode_requires_registered_device(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ing = _ingestor(db_session, monkeypatch)
    ing._require_registered_device = True
    topic = "mm/kn-zw-01/HT-102/position"
    # No device provisioned → rejected.
    ing._on_message(None, None, _Msg(topic, _BASE))
    assert ing.rejected_count == 1 and ing.stored_count == 0
    # Provision it, then the same message is accepted.
    s = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)()
    service.register_device(s, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    s.commit()
    s.close()
    ing._on_message(None, None, _Msg(topic, _BASE))
    assert ing.stored_count == 1


class _Client:
    """Records what the connect handler asks the paho client to do."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)


def _connack(name: str) -> ReasonCode:
    return ReasonCode(PacketTypes.CONNACK, name)


def _refusal_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "minemonitor.mqtt" and r.levelno == logging.ERROR and hasattr(r, "reason")
    ]


# paho 2.1 hands VERSION2 callbacks a CONNACK ReasonCode for MQTT 3.1.1 and v5 alike: the
# 3.1.1 return codes 4 and 5 arrive as these two names (values 134 and 135).
@pytest.mark.parametrize("refusal", ["Bad user name or password", "Not authorized"])
def test_refused_login_stops_the_ingestor_without_subscribing(
    refusal: str,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrong credentials looked like a quiet broker: no error, no data, nothing logged.
    Retrying cannot fix a refused login, so the ingestor stops (and exits non-zero)."""
    ing = _ingestor(db_session, monkeypatch)
    ing.host, ing.port = "broker.example", 1883
    client = _Client()
    with caplog.at_level(logging.ERROR, logger="minemonitor.mqtt"):
        ing._on_connect(client, None, None, _connack(refusal), None)
    assert client.subscribed == []
    [record] = _refusal_logs(caplog)
    assert record.reason == refusal  # type: ignore[attr-defined]
    assert (record.host, record.port) == ("broker.example", 1883)  # type: ignore[attr-defined]
    assert ing._stop.is_set()


def test_refused_login_makes_run_forever_exit_non_zero(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ing = _ingestor(db_session, monkeypatch)
    monkeypatch.setattr(
        ing,
        "start",
        lambda: ing._on_connect(_Client(), None, None, _connack("Not authorized"), None),
    )
    exit_code: list[object] = []

    def run() -> None:
        try:
            ing.run_forever()
        except SystemExit as exc:
            exit_code.append(exc.code)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    runner.join(timeout=5)
    ing._stop.set()  # unblock a run_forever that ignored the refusal
    runner.join(timeout=5)
    assert len(exit_code) == 1 and exit_code[0] not in (0, None)


def test_other_connack_failure_is_logged_and_left_to_reconnect(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ing = _ingestor(db_session, monkeypatch)
    ing.host, ing.port = "broker.example", 1883
    client = _Client()
    with caplog.at_level(logging.ERROR, logger="minemonitor.mqtt"):
        ing._on_connect(client, None, None, _connack("Server unavailable"), None)
    assert client.subscribed == []
    [record] = _refusal_logs(caplog)
    assert record.reason == "Server unavailable"  # type: ignore[attr-defined]
    assert (record.host, record.port) == ("broker.example", 1883)  # type: ignore[attr-defined]
    assert not ing._stop.is_set()  # paho's own reconnect carries on


def test_accepted_login_subscribes_as_before(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ing = _ingestor(db_session, monkeypatch)
    client = _Client()
    with caplog.at_level(logging.ERROR, logger="minemonitor.mqtt"):
        ing._on_connect(client, None, None, _connack("Success"), None)
    assert client.subscribed == ["mm/+/+/position", "mm/+/vision/+/+"]
    assert _refusal_logs(caplog) == []
    assert not ing._stop.is_set()


def test_publisher_logs_a_refused_login(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The simulator and Teltonika adapter publish through MqttPublisher: a refused login
    there left positions piling up in the spool with nothing in the logs."""
    pub = MqttPublisher(host="broker.example", port=1883, spool_path=str(tmp_path / "spool.db"))
    try:
        with caplog.at_level(logging.ERROR, logger="minemonitor.mqtt"):
            pub._on_connect(None, None, None, _connack("Not authorized"), None)
        assert not pub._connected.is_set()
        [record] = _refusal_logs(caplog)
        assert record.reason == "Not authorized"  # type: ignore[attr-defined]
        assert (record.host, record.port) == ("broker.example", 1883)  # type: ignore[attr-defined]
    finally:
        pub.spool.close()
