"""MQTT ingestor enforcement: topic/payload anti-spoof + strict device mode (§10)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.devices import service
from minemonitor.ingest.mqtt import MqttIngestor


class _Msg:
    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()


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
