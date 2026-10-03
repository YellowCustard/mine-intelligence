"""Device-supplied ids are constrained at the ingest boundary.

Regression for a stored-XSS path: a device account could ingest a position whose
``asset_id`` carried markup, which the dashboard then rendered for every viewer. Ids
now share one conservative token alphabet across the JSON Schema, the Pydantic
contract, HTTP ingest and MQTT ingest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.contracts import AssetPositionV1
from minemonitor.ingest.mqtt import MqttIngestor
from tests.conftest import CONTRACTS_DIR, DEVICE, make_client

_HOSTILE = [
    "HT-102\n",  # Python re: `$` also matches before a trailing newline
    "<img src=x onerror=alert(1)>",
    "x');alert(1);('",
    'a"b',
    "HT 102",
    "mm/+/#",
    "-leading-dash",
    "",
    "a" * 65,
]
_VALID = ["HT-102", "a", "teltonika-356307042441013", "kn-zw-01", "cam.01:north_2", "A" * 64]


def _position(asset_id: str) -> dict[str, object]:
    return {
        "schema": "asset.position.v1",
        "site_id": "kn-zw-01",
        "asset_id": asset_id,
        "ts": "2026-09-05T11:42:07Z",
        "received_at": "2026-09-05T11:42:08Z",
        "lat": -17.8252,
        "lon": 31.0335,
    }


def _schema() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(Path(CONTRACTS_DIR, "asset.position.v1.json").read_text())
    )


@pytest.mark.parametrize("asset_id", _HOSTILE)
def test_contract_and_schema_reject_hostile_ids(asset_id: str) -> None:
    with pytest.raises(ValidationError):
        AssetPositionV1.model_validate(_position(asset_id))
    with pytest.raises(SchemaError):
        _schema().validate(_position(asset_id))


@pytest.mark.parametrize("asset_id", _VALID)
def test_contract_and_schema_accept_real_ids(asset_id: str) -> None:
    AssetPositionV1.model_validate(_position(asset_id))
    _schema().validate(_position(asset_id))


@pytest.mark.parametrize("asset_id", _HOSTILE)
def test_http_ingest_rejects_hostile_asset_id(db_session: Session, asset_id: str) -> None:
    client = make_client(db_session, DEVICE)
    body = _position(asset_id)
    del body["received_at"]  # server-stamped; devices never send it
    r = client.post("/ingest/positions", json=body)
    assert r.status_code == 422


def test_http_ingest_rejects_hostile_site_id(db_session: Session) -> None:
    client = make_client(db_session, DEVICE)
    body = _position("HT-102")
    del body["received_at"]
    body["site_id"] = "<svg onload=alert(1)>"
    assert client.post("/ingest/positions", json=body).status_code == 422


class _Msg:
    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()


def test_mqtt_ingest_rejects_hostile_asset_id(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    ing = MqttIngestor()
    ing.prefix = "mm"
    body = _position("<img src=x onerror=alert(1)>")
    del body["received_at"]
    body["ts"] = datetime.now(UTC).isoformat()
    ing._on_message(None, None, _Msg("mm/kn-zw-01/<img src=x onerror=alert(1)>/position", body))
    assert ing.rejected_count == 1 and ing.stored_count == 0
