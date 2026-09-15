"""In-process event bus (Phase 1 foundation).

A minimal synchronous publish/subscribe seam so future domains can react to events
without importing one another. It is deliberately in-process and synchronous — no
new broker, no new runtime dependency, so the mine's offline-first core is unchanged.
When a real forcing function appears (edge isolation, independent scaling), a domain
can move behind MQTT or a service using the *same* contract; the bus is the seam that
makes that migration additive.

Design choices that keep it safe to adopt incrementally:

- Publish is synchronous and best-effort: a failing subscriber is isolated, logged
  and counted, and never breaks the publisher or the other subscribers.
- Every published event is a registered contract model; the schema string is read
  from the model, so routing and metrics stay consistent with the registry.
- The existing ingest/pipeline hot path is not wired through this yet (behaviour is
  preserved); the bus is available for new domains and opt-in wiring.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel

from minemonitor.platform.metrics import metrics

log = logging.getLogger("minemonitor.platform.bus")

Handler = Callable[[BaseModel], None]


def _schema_of(event: BaseModel) -> str:
    schema = getattr(event, "schema_", None)
    if not isinstance(schema, str) or not schema:
        raise ValueError(f"{type(event).__name__} has no schema_ field; not a versioned contract")
    return schema


class EventBus:
    """Synchronous, error-isolated in-process pub/sub over versioned contracts."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = {}

    def subscribe(self, schema: str, handler: Handler) -> None:
        self._subscribers.setdefault(schema, []).append(handler)

    def publish(self, event: BaseModel) -> int:
        """Deliver ``event`` to every subscriber of its schema. Returns the count
        delivered without error. A subscriber that raises is isolated and counted."""
        schema = _schema_of(event)
        m = metrics()
        m.incr(f"bus.published.{schema}")
        delivered = 0
        for handler in self._subscribers.get(schema, ()):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - isolate a bad subscriber
                m.incr(f"bus.failed.{schema}")
                log.warning(
                    "event subscriber failed",
                    extra={
                        "schema": schema,
                        "handler": getattr(handler, "__name__", "?"),
                        "error": str(exc),
                    },
                )
                continue
            delivered += 1
            m.incr(f"bus.delivered.{schema}")
        return delivered

    def clear(self) -> None:
        """Drop all subscribers (tests)."""
        self._subscribers.clear()


# Process-wide default bus.
_BUS = EventBus()


def bus() -> EventBus:
    return _BUS
