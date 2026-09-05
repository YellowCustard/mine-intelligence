"""MQTT transport: a store-and-forward publisher and an idempotent ingestor.

Design against the two failures the mine will actually see (brief §3):

* **Broker restart** — the publisher writes every position to a crash-safe spool
  first, forwards from it at QoS 1, and deletes only on the broker's PUBACK.
  Anything unacknowledged stays in the spool and is re-sent on reconnect. No loss.

* **Database restart** — the ingestor does not acknowledge a message until it has
  been durably stored. It retries the write until the database returns, so the
  broker holds unacknowledged messages (persistent session, QoS 1) rather than
  the consumer dropping them. Stores are idempotent, so redelivery never
  duplicates. No loss, no duplicates.
"""

from __future__ import annotations

import logging
import threading
import time

import paho.mqtt.client as mqtt

from minemonitor.config import get_settings
from minemonitor.ingest.service import PositionIngest, store_and_process
from minemonitor.ingest.spool import Spool
from minemonitor.storage.db import get_session_factory

log = logging.getLogger("minemonitor.mqtt")


def position_topic(prefix: str, site_id: str, asset_id: str) -> str:
    """Topic for an asset's position stream."""
    return f"{prefix}/{site_id}/{asset_id}/position"


class MqttPublisher:
    """Publishes positions with a durable spool in front of the broker."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        prefix: str | None = None,
        spool_path: str | None = None,
        client_id: str = "mm-publisher",
        drain_interval_s: float = 0.5,
    ) -> None:
        s = get_settings()
        self.host = host or s.mqtt_host
        self.port = port or s.mqtt_port
        self.prefix = prefix or s.mqtt_topic_prefix
        self.spool = Spool(spool_path or s.spool_path)
        self.drain_interval_s = drain_interval_s
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._connected = threading.Event()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._stop = threading.Event()
        self._drainer: threading.Thread | None = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            self._connected.set()
            log.info("publisher connected", extra={"host": self.host})

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected.clear()
        log.warning("publisher disconnected")

    def start(self) -> None:
        self._client.connect_async(self.host, self.port)
        self._client.loop_start()
        self._drainer = threading.Thread(target=self._drain_loop, daemon=True)
        self._drainer.start()

    def publish_position(self, payload: PositionIngest) -> None:
        """Durably enqueue a position. Delivery happens from the spool."""
        self.spool.append(payload.model_dump_json(by_alias=True))

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            if not self._connected.is_set():
                time.sleep(self.drain_interval_s)
                continue
            delivered = self._drain_once()
            if not delivered:
                time.sleep(self.drain_interval_s)

    def _drain_once(self, batch: int = 200) -> int:
        """Forward one batch from the spool; delete only on confirmed delivery."""
        entries = self.spool.peek(batch)
        done: list[int] = []
        for spool_id, raw in entries:
            payload = PositionIngest.model_validate_json(raw)
            topic = position_topic(self.prefix, payload.site_id, payload.asset_id)
            info = self._client.publish(topic, raw, qos=1)
            try:
                info.wait_for_publish(timeout=5.0)
            except (ValueError, RuntimeError):
                break  # not connected / queue full — retry this entry later
            if info.is_published():
                done.append(spool_id)
            else:
                break
        self.spool.delete(done)
        return len(done)

    def stop(self) -> None:
        self._stop.set()
        if self._drainer is not None:
            self._drainer.join(timeout=2.0)
        self._client.loop_stop()
        self._client.disconnect()
        self.spool.close()


class MqttIngestor:
    """Subscribes to position topics and stores each message idempotently."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        prefix: str | None = None,
        client_id: str | None = None,
        write_retry_s: float = 1.0,
    ) -> None:
        s = get_settings()
        self.host = host or s.mqtt_host
        self.port = port or s.mqtt_port
        self.prefix = prefix or s.mqtt_topic_prefix
        self.write_retry_s = write_retry_s
        self._session_factory = get_session_factory()
        # Persistent session (clean_session=False + fixed id): the broker keeps
        # unacknowledged QoS-1 messages for us across a consumer restart.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or s.mqtt_ingest_client_id,
            clean_session=False,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._stop = threading.Event()
        self.stored_count = 0
        self._offline_thread: threading.Thread | None = None
        self._offline_threshold_s = s.offline_threshold_s
        self._offline_interval_s = s.offline_check_interval_s
        self._site_id = s.default_site_id

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        topic = f"{self.prefix}/+/+/position"
        client.subscribe(topic, qos=1)
        log.info("ingestor subscribed", extra={"topic": topic})

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = PositionIngest.model_validate_json(msg.payload)
        except ValueError:
            # Malformed device data: reject loudly, but do not block the queue by
            # withholding the ack — a bad message will never become good.
            log.error("rejected malformed position", extra={"topic": msg.topic})
            return
        # Withhold the ack (by not returning) until the write succeeds, so the
        # broker redelivers rather than the consumer dropping on a DB outage.
        self._store_with_retry(payload)
        self.stored_count += 1

    def _store_with_retry(self, payload: PositionIngest) -> None:
        while not self._stop.is_set():
            session = self._session_factory()
            try:
                created, events = store_and_process(session, payload)
                log.info(
                    "position ingested",
                    extra={
                        "site_id": payload.site_id,
                        "asset_id": payload.asset_id,
                        "source": "mqtt",
                        "position_created": created,
                        "events": len(events),
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - DB may be down; retry
                session.rollback()
                log.warning(
                    "store failed, will retry",
                    extra={"asset_id": payload.asset_id, "error": str(exc)},
                )
                time.sleep(self.write_retry_s)
            finally:
                session.close()

    def _offline_loop(self) -> None:
        """Periodic maintenance: offline detection and cycle/metric recompute.

        Cycles are recomputed from stored positions (not in the ingest hot path,
        brief §9), so the dashboard's cycle chart stays fresh without coupling
        analytics to ingest.
        """
        from minemonitor.cycles.recompute import recompute
        from minemonitor.rules.offline import detect_offline

        while not self._stop.wait(self._offline_interval_s):
            session = self._session_factory()
            try:
                events = detect_offline(
                    session, self._site_id, threshold_s=self._offline_threshold_s
                )
                for ev in events:
                    log.info(
                        "asset offline",
                        extra={"site_id": ev.site_id, "asset_id": ev.asset_id},
                    )
                recompute(session, self._site_id)
            except Exception as exc:  # noqa: BLE001 - transient DB errors; retry next tick
                session.rollback()
                log.warning("maintenance tick failed", extra={"error": str(exc)})
            finally:
                session.close()

    def start(self) -> None:
        self._client.connect(self.host, self.port)
        self._client.loop_start()
        self._offline_thread = threading.Thread(target=self._offline_loop, daemon=True)
        self._offline_thread.start()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._offline_thread is not None:
            self._offline_thread.join(timeout=2.0)
        self._client.loop_stop()
        self._client.disconnect()


def main() -> None:
    """Run the MQTT ingestor until interrupted."""
    from minemonitor.logging_config import configure_logging

    configure_logging(get_settings().log_level)
    MqttIngestor().run_forever()


if __name__ == "__main__":
    main()
