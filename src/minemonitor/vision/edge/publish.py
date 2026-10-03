"""Deliver the edge outbox to the core (stdlib; paho-mqtt for the MQTT publisher).

``drain`` moves pending records to the core in batches through any *publisher* — a callable taking
a list of payload dicts. The HTTP publisher posts to ``POST /api/v1/ingest/vision`` as the node's
``device`` account (HTTP Basic, the tracker credential model — no new auth surface). The MQTT
publisher sends each record at QoS 1 to ``<prefix>/<site>/vision/<camera_or_node>/<kind>`` as the
node's broker identity (a ``vision-edge`` device, ACL-confined to its site's vision topics).

Failure semantics (store-and-forward, ``CLAUDE.md`` §3):
- **Transient** (network down, 5xx, auth outage): stop, keep everything pending, retry next drain.
- **Rejected** (422 with the failing index): that one record is moved to ``dead`` with the reason
  and the rest are retried — a malformed record never blocks the queue behind it.
- Duplicates are harmless: the core is idempotent on every record id.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from minemonitor import net
from minemonitor.vision.edge.outbox import Outbox

Publisher = Callable[[list[dict[str, Any]]], dict[str, Any]]


class TransientPublishError(Exception):
    """The core is unreachable or erroring; keep records and retry later."""


class RejectedRecordError(Exception):
    """The core refused one record of the batch (malformed, unknown camera/rule)."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(f"record {index} rejected: {reason}")
        self.index = index
        self.reason = reason


def drain(outbox: Outbox, publish: Publisher, *, batch: int = 100) -> dict[str, int]:
    """Send pending records until empty or the core is unreachable. Returns counts."""
    sent = dead = 0
    while True:
        items = outbox.pending(batch)
        if not items:
            break
        payloads = [json.loads(p) for _, p in items]
        try:
            publish(payloads)
        except RejectedRecordError as exc:
            if not 0 <= exc.index < len(items):
                outbox.mark_attempt([i for i, _ in items], str(exc))
                break
            outbox.mark_dead(items[exc.index][0], exc.reason)
            dead += 1
            continue
        except TransientPublishError as exc:
            outbox.mark_attempt([i for i, _ in items], str(exc))
            break
        outbox.mark_sent([i for i, _ in items])
        sent += len(items)
    return {"sent": sent, "dead": dead, **{f"left_{k}": v for k, v in outbox.counts().items()}}


class HttpPublisher:
    """POST batches to the core's vision ingest endpoint as a device principal."""

    def __init__(self, base_url: str, username: str, password: str, *, timeout_s: float = 10.0):
        self.url = base_url.rstrip("/") + "/api/v1/ingest/vision"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {"Content-Type": "application/json", "Authorization": f"Basic {token}"}
        self.timeout_s = timeout_s

    def __call__(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url, data=json.dumps(payloads).encode(), headers=self._headers, method="POST"
        )
        try:
            with net.urlopen(req, timeout=self.timeout_s) as resp:
                return dict(json.loads(resp.read() or b"{}"))
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                try:
                    detail = json.loads(exc.read() or b"{}").get("detail", {})
                    raise RejectedRecordError(int(detail["index"]), str(detail["error"])) from exc
                except (ValueError, KeyError, TypeError):
                    raise TransientPublishError(f"422 without index: {exc}") from exc
            raise TransientPublishError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientPublishError(str(exc)) from exc


def vision_topic(prefix: str, payload: dict[str, Any]) -> str:
    """``<prefix>/<site>/vision/<camera_or_node>/<kind>`` — the topic the core's ingestor checks.

    Camera-scoped records publish under their camera, node health under the node, and the
    ``kind`` is the schema's middle part (``vision.zone_rollup.v1`` → ``zone_rollup``).
    """
    schema = str(payload.get("schema", ""))
    parts = schema.split(".")
    if len(parts) != 3 or parts[0] != "vision":
        raise RejectedRecordError(0, f"not a vision record: {schema!r}")
    source = payload.get("camera_id") or payload.get("node_id")
    return f"{prefix}/{payload['site_id']}/vision/{source}/{parts[1]}"


class MqttPublisher:
    """Publish records to the site broker at **QoS 1** as the node's broker identity.

    For sites where the node reaches the broker but not the API. Each record is confirmed by the
    broker (PUBACK) before the batch counts as sent; anything unconfirmed stays in the outbox and
    is retried — the core dedupes on record id, so a resend is harmless. The broker ACL confines
    the node to ``<prefix>/<site>/vision/#`` and the ingestor rejects a topic/payload mismatch.

    Over MQTT the core cannot answer "record 3 is malformed", so a rejected record is logged by the
    ingestor rather than dead-lettered here; the HTTP publisher remains the commissioning default.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        client_id: str,
        prefix: str = "mm",
        timeout_s: float = 10.0,
    ) -> None:
        import paho.mqtt.client as mqtt

        self.prefix = prefix
        self.timeout_s = timeout_s
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False
        )
        self._client.username_pw_set(username, password)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        # The outbox is the durable queue. Bound paho's own queue so an outage cannot grow memory.
        self._client.max_queued_messages_set(1000)
        self._host = host
        self._port = port
        self._started = False

    def _ensure(self) -> None:
        if self._started:
            return
        try:
            self._client.connect(self._host, self._port, keepalive=30)
        except OSError as exc:
            raise TransientPublishError(f"broker unreachable: {exc}") from exc
        self._client.loop_start()
        self._started = True
        deadline = time.monotonic() + self.timeout_s
        while not self._client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)  # first CONNACK; later reconnects happen in paho's loop thread

    def __call__(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        self._ensure()
        if not self._client.is_connected():
            # Publishing now would only pile copies into paho's queue; the outbox keeps the batch.
            raise TransientPublishError("broker not connected")
        infos = []
        for i, p in enumerate(payloads):
            try:
                topic = vision_topic(self.prefix, p)
            except (RejectedRecordError, KeyError) as exc:
                raise RejectedRecordError(i, str(exc)) from exc
            infos.append(self._client.publish(topic, json.dumps(p), qos=1))
        for info in infos:
            try:
                info.wait_for_publish(timeout=self.timeout_s)
            except (RuntimeError, ValueError) as exc:  # not queued / client disconnected
                raise TransientPublishError(f"publish failed: {exc}") from exc
            if not info.is_published():
                raise TransientPublishError("broker did not confirm within the timeout")
        return {"published": len(infos)}

    def close(self) -> None:
        if self._started:
            self._client.loop_stop()
            self._client.disconnect()
            self._started = False
