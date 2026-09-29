"""Unit tests for the pure vision modules: geometry, temporal state machine, rollup coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from minemonitor.contracts.vision import VisionObservationV1, VisionRuleVersionV1
from minemonitor.vision.adapters.base import Detection
from minemonitor.vision.geometry import (
    apply_homography,
    foot_point,
    iou,
    point_in_polygon,
    validate_homography,
    validate_polygon,
    zones_containing,
)
from minemonitor.vision.rollup import RollupAccumulator
from minemonitor.vision.temporal import TemporalEngine, replay

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
SQUARE = [[0.5, 0.5], [0.9, 0.5], [0.9, 0.9], [0.5, 0.9]]


# -- geometry ----------------------------------------------------------------------------------


def test_foot_point_and_iou() -> None:
    assert foot_point((0.2, 0.1, 0.4, 0.5)) == pytest.approx((0.3, 0.5))
    assert iou((0, 0, 0.5, 0.5), (0, 0, 0.5, 0.5)) == pytest.approx(1.0)
    assert iou((0, 0, 0.2, 0.2), (0.5, 0.5, 0.7, 0.7)) == 0.0
    assert iou((0, 0, 0.4, 0.4), (0.2, 0.2, 0.6, 0.6)) == pytest.approx(0.04 / 0.28)


def test_point_in_polygon_and_zones_use_foot_point() -> None:
    assert point_in_polygon(0.7, 0.7, SQUARE) and not point_in_polygon(0.3, 0.7, SQUARE)
    assert not point_in_polygon(0.5, 0.5, [[0, 0], [1, 1]])  # degenerate
    zones = [{"zone_id": "z", "polygon": SQUARE}]
    # head inside the zone but feet outside → not in the zone
    assert zones_containing((0.6, 0.55, 0.7, 0.95), zones) == []
    assert zones_containing((0.6, 0.3, 0.7, 0.7), zones) == ["z"]


def test_polygon_and_homography_validation() -> None:
    assert validate_polygon([[0, 0], [1, 0], [1, 1]]) == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
    for bad in ([[0, 0], [1, 1]], [[0, 0], [1, 0], [1.5, 1]], "x", [[0, 0, 0], [1, 0], [1, 1]]):
        with pytest.raises(ValueError):
            validate_polygon(bad)
    with pytest.raises(ValueError):
        validate_homography({"matrix": [[1, 0], [0, 1]]})
    m = validate_homography({"matrix": [[1, 0, -17.9], [0, 1, 31.0], [0, 0, 1]]})
    assert apply_homography(m, 0.1, 0.2) == pytest.approx((-17.8, 31.2))
    assert apply_homography([[1, 0, 0], [0, 1, 0], [0, 0, 0]], 0.5, 0.5) is None  # horizon


def test_detection_rejects_out_of_ontology_and_bad_boxes() -> None:
    with pytest.raises(ValueError):
        Detection("face", 0.9, (0.1, 0.1, 0.2, 0.2))  # identity is not a perception class
    with pytest.raises(ValueError):
        Detection("person", 0.9, (0.3, 0.1, 0.2, 0.2))
    with pytest.raises(ValueError):
        Detection("person", 1.3, (0.1, 0.1, 0.2, 0.2))


# -- temporal state machine --------------------------------------------------------------------


def _rule(**params: Any) -> VisionRuleVersionV1:
    p = {
        "classes": ["person"],
        "min_observations": 3,
        "min_dwell_ms": 1000,
        "hysteresis_observations": 3,
        "cooldown_ms": 60_000,
        "window_ms": 5000,
        "confidence_threshold": 0.5,
        "severity": "critical",
        "zone_kinds": ["restricted"],
    }
    p.update(params)
    return VisionRuleVersionV1.model_validate(
        {
            "site_id": "s",
            "rule_id": "person_in_restricted_zone",
            "rule_version": "1.0.0",
            "event_type": "person_in_restricted_zone",
            "parameters": p,
            "active_from": "2026-09-01T00:00:00Z",
        }
    )


def _obs(
    i: int, *, inside: bool | None, conf: float = 0.9, track: str = "c:1"
) -> VisionObservationV1:
    objects = []
    if inside is not None:
        objects.append(
            {
                "track_id": track,
                "class": "person",
                "confidence": conf,
                "bbox": {"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.4},
                "zone_ids": ["r"] if inside else [],
                "provenance": "observed",
            }
        )
    return VisionObservationV1.model_validate(
        {
            "schema": "vision.observation.v1",
            "observation_id": f"o{i}",
            "site_id": "s",
            "node_id": "n",
            "camera_id": "c",
            "ts": T0 + timedelta(milliseconds=200 * i),
            "frame_id": f"f{i}",
            "model": {"id": "m", "version": "1"},
            "objects": objects,
            "advisory": True,
        }
    )


ZONES = {"r": {"kind": "restricted", "max_occupancy": None}}


def _run(pattern: list[bool | None], rule: VisionRuleVersionV1 | None = None) -> list[Any]:
    return replay(
        [_obs(i, inside=v) for i, v in enumerate(pattern)], rules=[rule or _rule()], zones=ZONES
    )


def test_fires_once_after_min_observations_and_dwell() -> None:
    evs = _run([True] * 20)  # 4 s inside at 5 fps
    assert len(evs) == 1
    e = evs[0]
    assert e.ts == T0 + timedelta(milliseconds=1000)  # frame 5: 3 obs confirmed, 1 s dwell met
    assert e.evidence.signals["dwell_ms"] == 1000 and e.provenance == "inferred"
    assert e.evidence.frame_ids[-1] == "f5"  # the confirming frame is in the evidence


def test_flicker_never_confirms() -> None:
    assert _run([True, False] * 20) == []
    assert _run([True, True, False] * 10) == []


def test_single_out_frame_after_confirmation_neither_clears_nor_refires() -> None:
    pattern: list[bool | None] = [True] * 10 + [False] + [True] * 10
    assert len(_run(pattern)) == 1


def test_release_after_hysteresis_then_cooldown_suppresses_reentry() -> None:
    pattern: list[bool | None] = [True] * 10 + [False] * 3 + [True] * 10
    assert len(_run(pattern)) == 1  # re-entry inside the 60 s cooldown: suppressed
    assert len(_run(pattern, _rule(cooldown_ms=0))) == 2  # no cooldown: a new presence alarms


def test_occlusion_keeps_state_but_long_absence_expires_it() -> None:
    occluded: list[bool | None] = [True, True] + [None] * 3 + [True] * 10
    assert len(_run(occluded)) == 1  # 3 unseen frames don't break the streak
    gone: list[bool | None] = [True, True] + [None] * 30 + [True]  # 6 s > window → expired
    assert _run(gone) == []


def test_low_confidence_and_wrong_class_ignored() -> None:
    low = [_obs(i, inside=True, conf=0.3) for i in range(20)]
    assert replay(low, rules=[_rule()], zones=ZONES) == []
    assert _run([True] * 20, _rule(classes=["haul_truck"])) == []


def test_zone_kind_filter() -> None:
    assert (
        replay(
            [_obs(i, inside=True) for i in range(20)],
            rules=[_rule()],
            zones={"r": {"kind": "generic", "max_occupancy": None}},
        )
        == []
    )


def test_event_ids_deterministic_and_versioned() -> None:
    a = _run([True] * 20)[0].event_id
    assert a == _run([True] * 20)[0].event_id
    v2 = _rule()
    v2 = v2.model_copy(update={"rule_version": "1.1.0"})
    assert _run([True] * 20, v2)[0].event_id != a


def test_engine_refuses_other_cameras_observation() -> None:
    eng = TemporalEngine(site_id="s", node_id="n", camera_id="other", rules=[_rule()], zones=ZONES)
    with pytest.raises(ValueError):
        eng.step(_obs(0, inside=True))


# -- rollup coverage ---------------------------------------------------------------------------


def test_rollup_caps_gaps_so_blind_time_is_not_coverage() -> None:
    acc = RollupAccumulator(site_id="s", node_id="n", camera_id="c", zone_ids=["r"], max_gap_s=2)
    for i in (0, 1, 2, 60, 61):  # a 58 s outage between frames 2 and 60
        acc.feed(_obs(i * 5, inside=True).model_copy(update={"ts": T0 + timedelta(seconds=i)}))
    (view, zone) = acc.flush(force=True)
    assert zone.zone_id == "r" and zone.observed_s == pytest.approx(5.0)  # 1+1+2(capped)+1
    assert view.zone_id is None and view.count_max == 1
