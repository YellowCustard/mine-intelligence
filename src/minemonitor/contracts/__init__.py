"""Pydantic v2 models mirroring the JSON Schema contracts in ``contracts/``.

The JSON Schema files are the published source of truth; these models are the
runtime mirror validated at every ingest boundary. ``tests/test_contracts.py``
asserts the two stay compatible so they cannot silently drift.
"""

from minemonitor.contracts.event import EventV1
from minemonitor.contracts.metrics import AssetMetricsV1
from minemonitor.contracts.position import AssetPositionV1

__all__ = ["AssetPositionV1", "EventV1", "AssetMetricsV1"]
