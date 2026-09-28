"""Synthetic vision scenarios — the hardware-free fixture set for Stage B. Pure Python.

The vision counterpart of ``ingest/adapters/simulator.py``: plausible per-frame **detections**
(not pixels) for scripted scenes, so the whole pipeline — tracker, temporal rules, rollups, edge
outbox, core ingest, alarm queue — is developable and testable with no camera, no GPU and no model
weights (``docs/VISION_BUILD_GATE.md`` §9). Deterministic for a given seed.

Each scenario is a set of actors moving on a camera's image plane. An actor's position is its
**foot point** (bottom-centre of its box), which is what the image-zones test. The scene has two
zones: a restricted zone on the right and a generic yard (capacity 2) on the left.

Scenarios and what the acceptance tests expect of them:

- ``intruder``            one person walks into the restricted zone and stays → exactly one alarm
- ``hugger``              a person's foot point flickers across the restricted edge → no alarm
- ``blink``               a single-frame false detection inside the zone → no alarm
- ``occluded_intruder``   an intruder hidden for 1 s mid-stay → still exactly one alarm
- ``loiterer``            a person stands in the yard for 150 s → one ``person_dwell``
- ``crowd``               three people in a yard capped at two → one ``zone_occupancy_exceeded``
- ``crossing``            two people cross paths outside the zones → track ids stay stable
- ``dark``                the camera goes dark for 40 s → one ``camera_obstructed``
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from minemonitor.vision.adapters.base import Detection, ModelInfo, VisionModel

RESTRICTED_POLYGON = [[0.55, 0.45], [0.95, 0.45], [0.95, 0.95], [0.55, 0.95]]
YARD_POLYGON = [[0.05, 0.45], [0.45, 0.45], [0.45, 0.95], [0.05, 0.95]]

# The simulated camera's scene config and zone policy (shape of GET .../vision/config).
SIM_IMAGE_ZONES: list[dict[str, Any]] = [
    {"zone_id": "sim-restricted", "zone_version": "sim-1", "polygon": RESTRICTED_POLYGON},
    {"zone_id": "sim-yard", "zone_version": "sim-1", "polygon": YARD_POLYGON},
]
SIM_ZONES: dict[str, dict[str, Any]] = {
    "sim-restricted": {"name": "Sim Restricted", "kind": "restricted", "max_occupancy": None},
    "sim-yard": {"name": "Sim Yard", "kind": "generic", "max_occupancy": 2},
}
START = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)

Path = Callable[[float, int], tuple[float, float] | None]  # (t_seconds, frame_index) -> foot point


@dataclass(frozen=True)
class Actor:
    name: str
    cls: str
    path: Path
    w: float = 0.06
    h: float = 0.2
    confidence: float = 0.85
    jitter: float = 0.003


@dataclass(frozen=True)
class SimFrame:
    """One simulated frame: its detections, the ground-truth actor behind each, camera state."""

    ts: datetime
    frame_id: str
    detections: tuple[Detection, ...]
    truth: tuple[str, ...]
    camera_state: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_s: float
    fps: float
    actors: tuple[Actor, ...] = ()
    camera_state: Callable[[float], str | None] | None = None
    extra_frames: dict[int, tuple[Detection, ...]] = field(default_factory=dict)


def _lerp(a: tuple[float, float], b: tuple[float, float], u: float) -> tuple[float, float]:
    u = max(0.0, min(1.0, u))
    return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)


def _walk_then_stay(
    a: tuple[float, float], b: tuple[float, float], walk_s: float, hidden: tuple[float, float]
) -> Path:
    def path(t: float, _i: int) -> tuple[float, float] | None:
        if hidden[0] <= t < hidden[1]:
            return None
        return _lerp(a, b, t / walk_s)

    return path


def _scenarios() -> dict[str, Scenario]:
    never = (0.0, 0.0)
    return {
        "intruder": Scenario(
            "intruder",
            16,
            5,
            (Actor("p1", "person", _walk_then_stay((0.3, 0.3), (0.75, 0.7), 4, never)),),
        ),
        "hugger": Scenario(
            "hugger",
            20,
            5,
            (
                Actor(
                    "p1",
                    "person",
                    lambda t, i: (0.55 + (0.012 if i % 2 else -0.012), 0.7),
                    jitter=0.0,
                ),
            ),
        ),
        "blink": Scenario(
            "blink",
            10,
            5,
            (),
            extra_frames={
                10: (Detection("person", 0.9, (0.72, 0.5, 0.78, 0.7)),),
            },
        ),
        "occluded_intruder": Scenario(
            "occluded_intruder",
            16,
            5,
            (Actor("p1", "person", _walk_then_stay((0.3, 0.3), (0.75, 0.7), 4, (8.0, 9.0))),),
        ),
        "loiterer": Scenario(
            "loiterer",
            150,
            2,
            (Actor("p1", "person", lambda t, i: (0.25, 0.7)),),
        ),
        "crowd": Scenario(
            "crowd",
            14,
            5,
            (
                Actor("p1", "person", _walk_then_stay((0.1, 0.3), (0.15, 0.7), 3, never)),
                Actor("p2", "person", _walk_then_stay((0.25, 0.3), (0.27, 0.8), 3, never)),
                Actor("p3", "person", _walk_then_stay((0.4, 0.3), (0.38, 0.6), 3, never)),
            ),
        ),
        "crossing": Scenario(
            "crossing",
            10,
            5,
            (
                Actor("a", "person", lambda t, i: _lerp((0.1, 0.30), (0.9, 0.30), t / 10)),
                Actor("b", "person", lambda t, i: _lerp((0.9, 0.34), (0.1, 0.34), t / 10)),
            ),
        ),
        "dark": Scenario(
            "dark",
            45,
            1,
            (),
            camera_state=lambda t: "dark" if 2 <= t < 42 else "ok",
        ),
    }


SCENARIOS = _scenarios()


def frames(name: str, *, seed: int = 7, start: datetime = START) -> list[SimFrame]:
    """Render a scenario into frames. Deterministic for a given ``seed``."""
    sc = SCENARIOS[name]
    rng = random.Random(seed)
    n = int(sc.duration_s * sc.fps)
    out: list[SimFrame] = []
    for i in range(n):
        t = i / sc.fps
        dets: list[Detection] = []
        truth: list[str] = []
        for a in sc.actors:
            fp = a.path(t, i)
            if fp is None:
                continue
            fx = fp[0] + rng.gauss(0, a.jitter) if a.jitter else fp[0]
            fy = fp[1] + rng.gauss(0, a.jitter) if a.jitter else fp[1]
            box = (
                max(0.0, fx - a.w / 2),
                max(0.0, fy - a.h),
                min(1.0, fx + a.w / 2),
                min(1.0, fy),
            )
            dets.append(Detection(a.cls, a.confidence, box))
            truth.append(a.name)
        for d in sc.extra_frames.get(i, ()):
            dets.append(d)
            truth.append("false_positive")
        out.append(
            SimFrame(
                ts=start + timedelta(seconds=t),
                frame_id=f"{name}-{i:06d}",
                detections=tuple(dets),
                truth=tuple(truth),
                camera_state=sc.camera_state(t) if sc.camera_state else None,
            )
        )
    return out


class SimModel(VisionModel):
    """Stub adapter: returns the simulated detections carried on each :class:`SimFrame`.

    The default CI "model" — weights-free, GPU-free — satisfying the same interface a real
    detector does, so everything downstream is exercised unchanged.
    """

    def info(self) -> ModelInfo:
        return ModelInfo(
            id="stub-sim",
            version="0.1.0",
            task="detect",
            licence="Apache-2.0",
            classes=("person", "haul_truck", "light_vehicle"),
            backend="python",
        )

    def detect(self, frame: Any) -> list[Detection]:
        if not isinstance(frame, SimFrame):
            raise TypeError("SimModel only understands SimFrame")
        return list(frame.detections)
