"""MQTT round-trip integration test.

Runs only when a broker is available (``MM_TEST_MQTT_HOST`` set) and a PostgreSQL
URL is configured (``MM_DATABASE_URL``). Exercises the real path: publisher →
broker → ingestor → database, including idempotent redelivery. CI provides both a
Mosquitto and a TimescaleDB service.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

_MQTT = os.environ.get("MM_TEST_MQTT_HOST")
_DB = os.environ.get("MM_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _MQTT or not _DB.startswith("postgresql"),
    reason="set MM_TEST_MQTT_HOST and a PostgreSQL MM_DATABASE_URL to run",
)


@pytest.fixture
def clean_positions() -> Iterator[None]:
    engine = create_engine(_DB, future=True)
    # Ensure the schema exists (CI runs the migration first; be defensive anyway).
    from minemonitor.storage.models import Base

    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DELETE FROM positions"))
    yield
    engine.dispose()


def _count(distinct: bool = False) -> int:
    engine = create_engine(_DB, future=True)
    try:
        with engine.connect() as c:
            if distinct:
                q = "SELECT count(*) FROM (SELECT DISTINCT site_id, asset_id, ts FROM positions) q"
            else:
                q = "SELECT count(*) FROM positions"
            return int(c.execute(text(q)).scalar_one())
    finally:
        engine.dispose()


def test_publish_ingest_roundtrip_and_idempotent(clean_positions: None) -> None:
    from minemonitor.ingest.adapters.simulator import Simulator
    from minemonitor.ingest.mqtt import MqttIngestor, MqttPublisher

    run = uuid.uuid4().hex[:8]
    sim = Simulator(seed=5)
    publisher = MqttPublisher(
        spool_path=f"/tmp/mm-test-spool-{run}.sqlite", client_id=f"mm-pub-{run}"
    )
    ingestor = MqttIngestor(client_id=f"mm-ing-{run}", write_retry_s=0.5)
    ingestor.start()
    publisher.start()
    time.sleep(1.0)

    payloads = [p for _ in range(3) for p in sim.step()]  # 3 ticks x 9 assets
    for p in payloads:
        publisher.publish_position(p)

    deadline = time.time() + 20
    while time.time() < deadline and _count() < len(payloads):
        time.sleep(0.5)
    assert _count() == len(payloads)
    assert _count(distinct=True) == len(payloads)

    # Replay the same payloads — idempotent, no duplicates.
    for p in payloads:
        publisher.publish_position(p)
    time.sleep(2.0)
    assert _count() == len(payloads)

    publisher.stop()
    ingestor.stop()
