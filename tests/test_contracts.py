"""The Pydantic models must stay compatible with the published JSON Schema.

We validate representative instances against BOTH the committed JSON Schema and
the Pydantic model. If a field is added to one and not the other, an instance
that exercises it fails on the side that is behind — catching silent drift.
"""

from __future__ import annotations

import json
from typing import get_args

import jsonschema
import pytest
from jsonschema import Draft202012Validator

from minemonitor.contracts import AssetMetricsV1, AssetPositionV1, EventV1
from minemonitor.contracts.event import EventType
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


def test_event_types_match_the_published_schema() -> None:
    # A type added to one list and not the other would be refused on one side only.
    enum = _load_schema("event.v1.json")["properties"]["type"]["enum"]
    assert sorted(get_args(EventType)) == sorted(enum)


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


def test_fuel_transaction_valid_instance_passes_both() -> None:
    from minemonitor.contracts.fuel import FuelTransactionV1

    Draft202012Validator.check_schema(_load_schema("fuel.transaction.v1.json"))
    instance = {
        "schema": "fuel.transaction.v1",
        "transaction_id": "01J9Z8ABCDEF",
        "site_id": "kn-zw-01",
        "ts": "2026-09-05T11:42:07Z",
        "litres": 120.5,
        "direction": "dispense",
        "source": "manual",
        "asset_id": "HT-102",
        "measured": True,
    }
    Draft202012Validator(_load_schema("fuel.transaction.v1.json")).validate(instance)
    model = FuelTransactionV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("fuel.transaction.v1.json")).validate(dumped)


def test_weighbridge_transaction_valid_instance_passes_both() -> None:
    from minemonitor.contracts.weighbridge import WeighbridgeTransactionV1

    Draft202012Validator.check_schema(_load_schema("weighbridge.transaction.v1.json"))
    instance = {
        "schema": "weighbridge.transaction.v1",
        "ticket_id": "01J9Z8ABCDEF",
        "site_id": "kn-zw-01",
        "ticket_no": "T-500",
        "ts": "2026-09-05T11:42:07Z",
        "direction": "outbound",
        "gross_kg": 40000,
        "tare_kg": 15000,
        "net_kg": 25000,
        "material": "gold_ore",
        "measured": True,
    }
    Draft202012Validator(_load_schema("weighbridge.transaction.v1.json")).validate(instance)
    model = WeighbridgeTransactionV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("weighbridge.transaction.v1.json")).validate(dumped)


def test_maintenance_health_valid_instance_passes_both() -> None:
    from minemonitor.contracts.maintenance import MaintenanceHealthV1

    Draft202012Validator.check_schema(_load_schema("maintenance.health.v1.json"))
    instance = {
        "schema": "maintenance.health.v1",
        "site_id": "kn-zw-01",
        "asset_id": "HT-102",
        "component": "engine",
        "ts": "2026-09-12T12:00:00Z",
        "risk": "High",
        "basis": "measured",
        "confidence": 0.85,
        "dimension": "hours",
        "fraction": 1.05,
        "remaining": -25.0,
        "evidence": ["525h since last service; interval 500h"],
        "recommended_action": "Service overdue — inspect and service promptly.",
        "inferred": True,
    }
    Draft202012Validator(_load_schema("maintenance.health.v1.json")).validate(instance)
    model = MaintenanceHealthV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("maintenance.health.v1.json")).validate(dumped)


def test_dispatch_recommendation_valid_instance_passes_both() -> None:
    from minemonitor.contracts.dispatch import DispatchRecommendationV1

    Draft202012Validator.check_schema(_load_schema("dispatch.recommendation.v1.json"))
    instance = {
        "schema": "dispatch.recommendation.v1",
        "assignment_id": "01J9Z8ABCDEF",
        "site_id": "kn-zw-01",
        "job_id": "01J9Z8JOB",
        "asset_id": "HT-102",
        "ts": "2026-09-12T12:00:00Z",
        "objective": "balanced",
        "score": 5.0,
        "rationale": ["job priority 5, material gold_ore", "HT-102 available"],
        "advisory": True,
    }
    Draft202012Validator(_load_schema("dispatch.recommendation.v1.json")).validate(instance)
    model = DispatchRecommendationV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("dispatch.recommendation.v1.json")).validate(dumped)


def test_access_event_valid_instance_passes_both() -> None:
    from minemonitor.contracts.access import AccessEventV1

    Draft202012Validator.check_schema(_load_schema("access.event.v1.json"))
    instance = {
        "schema": "access.event.v1",
        "event_id": "access-kn-zw-01-dahua_gate-g-1",
        "site_id": "kn-zw-01",
        "ts": "2026-09-12T06:02:11+02:00",
        "source_system": "dahua_gate",
        "gate_id": "main-gate",
        "credential_ref": "face-evt-5521",
        "operator_ref": "OP-001",
        "decision": "granted",
        "reason": None,
        "search_selected": True,
        "search_completed": True,
        "metal_detected": False,
        "advisory": True,
    }
    Draft202012Validator(_load_schema("access.event.v1.json")).validate(instance)
    model = AccessEventV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("access.event.v1.json")).validate(dumped)


def test_laboratory_result_valid_instance_passes_both() -> None:
    from minemonitor.contracts.laboratory import LaboratoryResultV1

    Draft202012Validator.check_schema(_load_schema("laboratory.result.v1.json"))
    instance = {
        "schema": "laboratory.result.v1",
        "result_id": "lab-kn-zw-01-agilent-aa-2000-S-1001-Au",
        "site_id": "kn-zw-01",
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-1001",
        "ts": "2026-09-12T09:15:00Z",
        "element": "Au",
        "value": 3.42,
        "unit": "g/t",
        "method": "fire_assay_aas",
        "batch_ref": "RUN-88",
        "original_hash": "a" * 64,
        "original_file_ref": None,
        "source": "spectraa_csv",
        "provenance": "measured",
    }
    Draft202012Validator(_load_schema("laboratory.result.v1.json")).validate(instance)
    model = LaboratoryResultV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("laboratory.result.v1.json")).validate(dumped)


def test_laboratory_result_provenance_must_be_measured() -> None:
    """A lab result is a measured instrument value — provenance cannot be anything else."""
    from minemonitor.contracts.laboratory import LaboratoryResultV1

    instance = {
        "schema": "laboratory.result.v1",
        "result_id": "lab-1",
        "site_id": "kn-zw-01",
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-1",
        "ts": "2026-09-12T09:15:00Z",
        "element": "Au",
        "value": 1.0,
        "unit": "g/t",
        "original_hash": "b" * 64,
        "provenance": "estimated",
    }
    with pytest.raises(Exception):
        LaboratoryResultV1.model_validate(instance)
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("laboratory.result.v1.json")).validate(instance)


def test_laboratory_correction_valid_instance_passes_both() -> None:
    from minemonitor.contracts.laboratory import LaboratoryCorrectionV1

    Draft202012Validator.check_schema(_load_schema("laboratory.correction.v1.json"))
    instance = {
        "schema": "laboratory.correction.v1",
        "correction_id": "01J9Z8CORR",
        "site_id": "kn-zw-01",
        "result_id": "lab-kn-zw-01-agilent-aa-2000-S-1001-Au",
        "corrected_value": 3.51,
        "unit": None,
        "actor": "chemist-jane",
        "reason": "transcription of dilution factor corrected",
        "ts": "2026-09-12T14:00:00Z",
        "provenance": "corrected",
    }
    Draft202012Validator(_load_schema("laboratory.correction.v1.json")).validate(instance)
    model = LaboratoryCorrectionV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("laboratory.correction.v1.json")).validate(dumped)


def test_vision_vendor_event_valid_instance_passes_both() -> None:
    from minemonitor.contracts.vision import VisionVendorEventV1

    Draft202012Validator.check_schema(_load_schema("vision.vendor_event.v1.json"))
    instance = {
        "schema": "vision.vendor_event.v1",
        "vendor_event_id": "nvr-kn-zw-01-3-evt-1001",
        "site_id": "kn-zw-01",
        "source_system": "dahua_nvr",
        "source_event_id": "evt-1001",
        "camera_id": "CAM-03",
        "channel": "3",
        "vendor_type": "CrossRegionDetection",
        "normalized_type": "nvr_intrusion",
        "vendor_confidence": 0.91,
        "vendor_rule_name": "GoldRoom-Intrusion",
        "ts": "2026-09-12T22:14:07+02:00",
        "clip_ref": "nvr://ch3/x.mp4",
        "provenance": "vendor_inferred",
        "advisory": True,
    }
    Draft202012Validator(_load_schema("vision.vendor_event.v1.json")).validate(instance)
    model = VisionVendorEventV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("vision.vendor_event.v1.json")).validate(dumped)


def test_generator_reading_valid_instance_passes_both() -> None:
    from minemonitor.contracts.generator import GeneratorReadingV1

    Draft202012Validator.check_schema(_load_schema("generator.reading.v1.json"))
    instance = {
        "schema": "generator.reading.v1",
        "reading_id": "gen-kn-zw-01-GEN-01-20260912T040000Z",
        "site_id": "kn-zw-01",
        "asset_id": "GEN-01",
        "source": "dse_sim",
        "ts": "2026-09-12T06:00:00+02:00",
        "engine_hours": 1240.5,
        "running": True,
        "fuel_level_pct": 82.0,
        "expected_running": True,
        "fault": False,
        "fault_code": None,
        "measured": True,
    }
    Draft202012Validator(_load_schema("generator.reading.v1.json")).validate(instance)
    model = GeneratorReadingV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("generator.reading.v1.json")).validate(dumped)


def test_material_measurement_valid_instance_passes_both() -> None:
    from minemonitor.contracts.reconciliation import MaterialMeasurementV1

    Draft202012Validator.check_schema(_load_schema("material.measurement.v1.json"))
    instance = {
        "schema": "material.measurement.v1",
        "measurement_id": "mm-kn-zw-01-crusher-weightometer-W-500",
        "site_id": "kn-zw-01",
        "stage": "crusher",
        "ts": "2026-09-12T09:15:00Z",
        "quantity": 128.5,
        "unit": "t",
        "assay_ref": "lab-kn-zw-01-agilent-aa-2000-S-1001-Au",
        "quality": "ok",
        "source": "weightometer",
        "provenance": "measured",
    }
    Draft202012Validator(_load_schema("material.measurement.v1.json")).validate(instance)
    model = MaterialMeasurementV1.model_validate(instance)
    dumped = json.loads(model.model_dump_json(by_alias=True))
    Draft202012Validator(_load_schema("material.measurement.v1.json")).validate(dumped)


def test_material_measurement_rejects_unknown_stage() -> None:
    from minemonitor.contracts.reconciliation import MaterialMeasurementV1

    instance = {
        "schema": "material.measurement.v1",
        "measurement_id": "mm-1",
        "site_id": "kn-zw-01",
        "stage": "smelter",  # not in the circuit enum
        "ts": "2026-09-12T09:15:00Z",
        "quantity": 1.0,
        "unit": "t",
        "provenance": "measured",
    }
    with pytest.raises(Exception):
        MaterialMeasurementV1.model_validate(instance)
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("material.measurement.v1.json")).validate(instance)


def test_vision_vendor_event_provenance_must_be_vendor_inferred() -> None:
    """An NVR event is a distinct provenance class — never first-party 'observed'."""
    from minemonitor.contracts.vision import VisionVendorEventV1

    instance = {
        "schema": "vision.vendor_event.v1",
        "vendor_event_id": "nvr-1",
        "site_id": "kn-zw-01",
        "source_system": "dahua_nvr",
        "source_event_id": "evt-1",
        "vendor_type": "CrossLineDetection",
        "normalized_type": "nvr_line_crossing",
        "ts": "2026-09-12T22:14:07+02:00",
        "provenance": "observed",
        "advisory": True,
    }
    with pytest.raises(Exception):
        VisionVendorEventV1.model_validate(instance)
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("vision.vendor_event.v1.json")).validate(instance)


# -- first-party vision contracts (Stage B) ------------------------------------------------------

_VISION_INSTANCES: dict[str, dict] = {
    "vision.observation.v1": {
        "schema": "vision.observation.v1",
        "observation_id": "vobs-cam-1-f1",
        "site_id": "kn-zw-01",
        "node_id": "edge-01",
        "camera_id": "cam-1",
        "ts": "2026-09-28T08:00:00Z",
        "received_at": None,
        "frame_id": "f1",
        "model": {"id": "yolox-nano", "version": "0.1.0"},
        "image": {"width": 1920, "height": 1080},
        "calibration_version": None,
        "objects": [
            {
                "track_id": "cam-1:7",
                "class": "person",
                "confidence": 0.8,
                "bbox": {"x1": 0.2, "y1": 0.2, "x2": 0.3, "y2": 0.6},
                "ground_point": {"lat": None, "lon": None, "basis": "unknown"},
                "zone_ids": ["gold-room"],
                "provenance": "observed",
            }
        ],
        "advisory": True,
    },
    "vision.operational_event.v1": {
        "schema": "vision.operational_event.v1",
        "event_id": "vop-1",
        "site_id": "kn-zw-01",
        "node_id": "edge-01",
        "camera_id": "cam-1",
        "ts": "2026-09-28T08:00:02Z",
        "type": "person_in_restricted_zone",
        "provenance": "inferred",
        "confidence": 0.81,
        "rule_id": "person_in_restricted_zone",
        "rule_version": "1.0.0",
        "subject": {"track_id": "cam-1:7", "asset_id": None, "association": None},
        "zone_id": "gold-room",
        "zone_version": "v1",
        "evidence": {
            "observation_window": ["2026-09-28T08:00:00Z", "2026-09-28T08:00:02Z"],
            "track_ids": ["cam-1:7"],
            "frame_ids": ["f1"],
            "signals": {"dwell_ms": 2000},
            "clip_ref": None,
        },
        "advisory": True,
    },
    "vision.health.v1": {
        "schema": "vision.health.v1",
        "health_id": "vh-1",
        "site_id": "kn-zw-01",
        "node_id": "edge-01",
        "ts": "2026-09-28T08:00:00Z",
        "status": "ok",
        "cameras_active": 1,
        "fps_mean": 4.2,
        "queue_depth": 0,
        "dropped_frames": 0,
        "cpu_pct": 41.0,
        "mem_pct": None,
        "gpu_pct": None,
        "model_ids": ["yolox-nano@0.1.0"],
        "last_error": None,
        "advisory": True,
    },
    "vision.camera_health.v1": {
        "schema": "vision.camera_health.v1",
        "health_id": "vch-1",
        "site_id": "kn-zw-01",
        "node_id": "edge-01",
        "camera_id": "cam-1",
        "ts": "2026-09-28T08:00:00Z",
        "state": "dark",
        "blur_score": 12.5,
        "luma_mean": 8.0,
        "frozen_frames": 0,
        "last_frame_at": "2026-09-28T07:59:59Z",
        "advisory": True,
    },
    "vision.rule_version.v1": {
        "schema": "vision.rule_version.v1",
        "site_id": "kn-zw-01",
        "rule_id": "person_in_restricted_zone",
        "rule_version": "1.2.0",
        "event_type": "person_in_restricted_zone",
        "parameters": {
            "classes": ["person"],
            "min_observations": 3,
            "min_dwell_ms": 1500,
            "hysteresis_observations": 3,
            "cooldown_ms": 60000,
            "window_ms": 5000,
            "confidence_threshold": 0.5,
            "severity": "critical",
            "zone_kinds": ["restricted"],
            "requires": [],
        },
        "zone_version": None,
        "active_from": "2026-10-01T00:00:00Z",
        "active_to": None,
        "author": "adeel",
        "notes": None,
        "advisory": True,
    },
    "vision.zone_rollup.v1": {
        "schema": "vision.zone_rollup.v1",
        "rollup_id": "vr-1",
        "site_id": "kn-zw-01",
        "node_id": "edge-01",
        "camera_id": "cam-1",
        "zone_id": None,
        "ts": "2026-09-28T08:00:00Z",
        "bucket_s": 300,
        "object_class": "person",
        "count_max": 3,
        "count_mean": 1.5,
        "entries": 4,
        "exits": 4,
        "dwell_s_max": 90.0,
        "dwell_s_p50": None,
        "observed_s": 288.0,
        "advisory": True,
    },
}


@pytest.mark.parametrize("schema", sorted(_VISION_INSTANCES))
def test_vision_contract_valid_instance_passes_both(schema: str) -> None:
    from minemonitor.platform.contracts import model_for

    js = _load_schema(f"{schema}.json")
    Draft202012Validator.check_schema(js)
    instance = _VISION_INSTANCES[schema]
    Draft202012Validator(js).validate(instance)
    model = model_for(schema)
    assert model is not None
    dumped = json.loads(model.model_validate(instance).model_dump_json(by_alias=True))
    Draft202012Validator(js).validate(dumped)


@pytest.mark.parametrize("schema", sorted(_VISION_INSTANCES))
def test_vision_contract_rejects_identity_fields_in_both(schema: str) -> None:
    """No vision contract admits identity or frame bytes (CLAUDE.md §4) — schema or model."""
    from minemonitor.platform.contracts import model_for

    model = model_for(schema)
    assert model is not None
    for extra in ({"person_name": "x"}, {"face_embedding": [0.1]}, {"frame_jpeg": "AAAA"}):
        bad = {**_VISION_INSTANCES[schema], **extra}
        with pytest.raises(jsonschema.ValidationError):
            Draft202012Validator(_load_schema(f"{schema}.json")).validate(bad)
        with pytest.raises(Exception):
            model.model_validate(bad)
