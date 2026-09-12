"""Process-local observability counters (Phase 1 foundation).

A tiny, dependency-free, thread-safe counter registry. It is intentionally
in-process and best-effort: counters reset on restart and are never a source of
truth — durable operational facts live in the database. Its job is to make event
flow and failures *visible* (a Command Centre / admin surface), complementing the
existing ``/health``, system-health and structured request-id logging.
"""

from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    """A thread-safe map of name → integer counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] += by

    def get(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        """A stable copy of all counters, sorted by name."""
        with self._lock:
            return dict(sorted(self._counters.items()))

    def reset(self) -> None:
        """Clear all counters (tests; not used in normal operation)."""
        with self._lock:
            self._counters.clear()


# Process-wide default registry.
_METRICS = Metrics()


def metrics() -> Metrics:
    return _METRICS
