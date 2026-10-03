"""One bad message must never wedge MQTT ingest.

Regression: ``ts`` accepted offset-less datetimes, which then crashed zone processing
(``TypeError`` comparing naive and aware datetimes). The MQTT ingestor retried *every*
exception forever inside the paho callback, so a single such message stopped ingest
for every asset behind it. Now naive timestamps are rejected at the contract, and only
transient database errors are retried.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import exc as sa_exc
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.contracts import AssetPositionV1
from minemonitor.ingest import mqtt as mqtt_mod
from minemonitor.ingest.mqtt import MqttIngestor
from minemonitor.ingest.service import PositionIngest
from minemonitor.storage.db import is_transient_db_error
from minemonitor.storage.models import Position
from tests.conftest import DEVICE, make_client

_TOPIC = "mm/kn-zw-01/HT-102/position"


class _Msg:
    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode()


def _body(ts: str) -> dict[str, object]:
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


def _iso(offset_s: int = 0) -> str:
    return (datetime(2026, 9, 5, 11, 0, tzinfo=UTC) + timedelta(seconds=offset_s)).isoformat()


@pytest.fixture
def ingestor(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> MqttIngestor:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.ingest.mqtt.get_session_factory", lambda: factory)
    ing = MqttIngestor(write_retry_s=0.0)
    ing.prefix = "mm"
    return ing


def _deliver(ing: MqttIngestor, msg: _Msg, timeout_s: float = 5.0) -> None:
    """Run the callback in a thread so a regression (infinite retry) fails, not hangs."""
    t = threading.Thread(target=ing._on_message, args=(None, None, msg), daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        ing._stop.set()  # release the stuck loop before failing
        t.join(2)
        pytest.fail("ingest callback wedged on a single message")


def _stored(db_session: Session) -> int:
    return len(db_session.execute(select(Position)).all())


def test_contracts_reject_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        PositionIngest.model_validate(_body("2026-09-05T11:42:07"))
    with pytest.raises(ValidationError):
        AssetPositionV1.model_validate({**_body(_iso()), "received_at": "2026-09-05T11:42:08"})
    PositionIngest.model_validate(_body("2026-09-05T13:42:07+02:00"))  # any offset is fine


def test_http_rejects_naive_timestamp(db_session: Session) -> None:
    client = make_client(db_session, DEVICE)
    assert client.post("/ingest/positions", json=_body("2026-09-05T11:42:07")).status_code == 422
    assert client.post("/ingest/positions", json=_body(_iso())).status_code == 202


def test_mqtt_naive_timestamp_is_rejected_not_wedging(
    ingestor: MqttIngestor, db_session: Session
) -> None:
    _deliver(ingestor, _Msg(_TOPIC, _body("2026-09-05T11:42:07")))
    _deliver(ingestor, _Msg(_TOPIC, _body("2026-09-05T11:42:08")))
    assert ingestor.rejected_count == 2
    _deliver(ingestor, _Msg(_TOPIC, _body(_iso())))
    assert ingestor.stored_count == 1 and _stored(db_session) == 1


def test_processing_bug_stores_raw_fix_and_moves_on(
    ingestor: MqttIngestor, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> list[object]:
        raise TypeError("deterministic processing bug")

    monkeypatch.setattr("minemonitor.pipeline.process_position", boom)
    _deliver(ingestor, _Msg(_TOPIC, _body(_iso(0))))
    _deliver(ingestor, _Msg(_TOPIC, _body(_iso(1))))
    assert ingestor.processing_failed_count == 2
    assert _stored(db_session) == 2  # raw telemetry kept; recompute can still use it


def test_transient_db_error_is_retried(
    ingestor: MqttIngestor, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = mqtt_mod.store_and_process
    calls = {"n": 0}

    def flaky(session: Session, payload: PositionIngest) -> tuple[bool, list[object]]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sa_exc.OperationalError("SELECT 1", {}, Exception("server closed connection"))
        return real(session, payload)

    monkeypatch.setattr(mqtt_mod, "store_and_process", flaky)
    _deliver(ingestor, _Msg(_TOPIC, _body(_iso())))
    assert calls["n"] == 3 and ingestor.processing_failed_count == 0
    assert _stored(db_session) == 1


def test_vision_deterministic_failure_does_not_wedge(
    ingestor: MqttIngestor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minemonitor.vision import service as vision

    monkeypatch.setattr(
        vision, "parse", lambda raw: SimpleNamespace(site_id="kn-zw-01", camera_id="cam-1")
    )

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise TypeError("deterministic")

    monkeypatch.setattr(vision, "ingest", boom)
    _deliver(ingestor, _Msg("mm/kn-zw-01/vision/cam-1/observation", {"schema": "x"}))
    assert ingestor.rejected_count == 1


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (sa_exc.OperationalError("q", {}, Exception("down")), True),
        (sa_exc.InterfaceError("q", {}, Exception("closed")), True),
        (sa_exc.TimeoutError("pool exhausted"), True),
        (ConnectionRefusedError(), True),
        (sa_exc.IntegrityError("q", {}, Exception("dup")), False),
        (sa_exc.DataError("q", {}, Exception("bad")), False),
        (TypeError("naive vs aware"), False),
        (ValueError("bad"), False),
    ],
)
def test_is_transient_db_error(exc: BaseException, transient: bool) -> None:
    assert is_transient_db_error(exc) is transient
