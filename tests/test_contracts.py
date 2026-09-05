"""The Pydantic models must stay compatible with the published JSON Schema.

We validate representative instances against BOTH the committed JSON Schema and
the Pydantic model. If a field is added to one and not the other, an instance
that exercises it fails on the side that is behind — catching silent drift.
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from minemonitor.contracts import AssetMetricsV1, AssetPositionV1, EventV1
from tests.conftest import CONTRACTS_DIR


def _load_schema(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / name).read_text())


def test_json_schemas_are_valid() -> None:
    for name in (
        "asset.position.v1.json",
        "event.v1.json",
        "asset.metrics.v1.json",
    ):
        Draft202012Validator.check_schema(_load_schema(name))


def test_position_valid_instance_passes_both() -> None:
    instance = {
        "schema": "asset.position.v1",
        "site_id": "kn-zw-01",
        "asset_id": "HT-102",
        "ts": "2026-09-05T11:42:07Z",
        "received_at": "2026-09-05T11:42:11Z",
        "lat": -17.8252,
        "lon": 31.0335,
        "altitude_m": 1483.2,
        "speed_kph": 47.0,
        "heading_deg": 118,
        "hdop": 0.9,
        "satellites": 11,
        "ignition": True,
        "source": "teltonika:fmb920",
    }
    Draft202012Validator(_load_schema("asset.position.v1.json")).validate(instance)
    model = AssetPositionV1.model_validate(instance)
    # Round-trip: the model re-serialises to a schema-valid instance.
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("asset.position.v1.json")).validate(dumped)


def test_position_rejects_out_of_range_lat() -> None:
    bad = {
        "schema": "asset.position.v1",
        "site_id": "kn-zw-01",
        "asset_id": "HT-102",
        "ts": "2026-09-05T11:42:07Z",
        "received_at": "2026-09-05T11:42:11Z",
        "lat": 999,
        "lon": 31.0,
    }
    with pytest.raises(Exception):
        AssetPositionV1.model_validate(bad)
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("asset.position.v1.json")).validate(bad)


def test_event_valid_instance_passes_both() -> None:
    instance = {
        "schema": "event.v1",
        "event_id": "01J9Z8ABCDEF",
        "site_id": "kn-zw-01",
        "ts": "2026-09-05T11:42:07Z",
        "type": "zone_breach",
        "severity": "critical",
        "asset_id": "LV-07",
        "zone_id": "r1-explosives-magazine",
        "source": "gnss_geofence",
        "summary": "LV-07 entered R1 Explosives Magazine without authorisation",
        "detail": {"dwell_s": 34, "speed_kph": 12.0},
        "evidence": {"positions": [], "clip_uri": None},
        "advisory": True,
        "state": "open",
        "acknowledged_by": None,
        "acknowledged_at": None,
    }
    Draft202012Validator(_load_schema("event.v1.json")).validate(instance)
    EventV1.model_validate(instance)


def test_event_advisory_must_be_true() -> None:
    """The advisory=True invariant travels with the data (brief §6/§15)."""
    instance = {
        "schema": "event.v1",
        "event_id": "01J9Z8ABCDEF",
        "site_id": "kn-zw-01",
        "ts": "2026-09-05T11:42:07Z",
        "type": "overspeed",
        "severity": "warning",
        "source": "gnss_geofence",
        "summary": "over limit",
        "advisory": False,
        "state": "open",
    }
    with pytest.raises(Exception):
        EventV1.model_validate(instance)
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("event.v1.json")).validate(instance)


def test_metrics_valid_instance_passes_both() -> None:
    instance = {
        "schema": "asset.metrics.v1",
        "site_id": "kn-zw-01",
        "asset_id": "HT-102",
        "bucket_start": "2026-09-05T11:40:00Z",
        "bucket_end": "2026-09-05T11:45:00Z",
        "distance_m": 1200.0,
        "moving_time_s": 240.0,
        "idle_time_s": 60.0,
        "max_speed_kph": 51.0,
        "mean_speed_kph": 30.0,
        "zone_dwell_s": {"rom-pad": 45.0},
        "loads_completed": 1,
    }
    Draft202012Validator(_load_schema("asset.metrics.v1.json")).validate(instance)
    AssetMetricsV1.model_validate(instance)
