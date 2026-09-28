"""Deliver the edge outbox to the core (stdlib only).

``drain`` moves pending records to the core in batches through any *publisher* — a callable taking
a list of payload dicts. The HTTP publisher posts to ``POST /api/v1/ingest/vision`` as the node's
``device`` account (HTTP Basic, the tracker credential model — no new auth surface).

Failure semantics (store-and-forward, ``CLAUDE.md`` §3):
- **Transient** (network down, 5xx, auth outage): stop, keep everything pending, retry next drain.
- **Rejected** (422 with the failing index): that one record is moved to ``dead`` with the reason
  and the rest are retried — a malformed record never blocks the queue behind it.
- Duplicates are harmless: the core is idempotent on every record id.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

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
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
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
