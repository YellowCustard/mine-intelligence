"""The model-adapter boundary (``docs/VISION_ARCHITECTURE.md`` §5). Pure Python.

Mine Monitor is **model-agnostic**: nothing downstream of this interface knows which model
produced a detection. Every adapter normalises to the same :class:`Detection` — class from the
perception ontology, normalised box, confidence, provenance ``observed`` — and nothing else. No
adapter emits an operational conclusion, an asset id, or anything about *who* a person is.

The interface is permissive-only by policy: an AGPL model (e.g. any Ultralytics YOLO) may not sit
behind it (``CLAUDE.md`` §7, ``docs/VISION_MODEL_LICENSES.md``). ``ModelInfo.licence`` travels
with every model so the registry can refuse a non-permissive one.

Tracking is a **separate layer** (``vision.tracker``), never folded into a detector, so a detector
swap keeps track ids and the two version independently.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from minemonitor.vision.geometry import BBox

# The perception ontology (docs/VISION_BUILD_GATE.md §6.1) — mirrors contracts.vision.ObjectClass.
OBJECT_CLASSES = frozenset(
    {
        "person",
        "haul_truck",
        "light_vehicle",
        "loader",
        "excavator",
        "dozer",
        "grader",
        "drill",
        "water_truck",
        "service_truck",
        "bus",
    }
)


@dataclass(frozen=True)
class Detection:
    """One object in one frame: class, normalised box, confidence. Perception only."""

    cls: str
    confidence: float
    bbox: BBox

    def __post_init__(self) -> None:
        if self.cls not in OBJECT_CLASSES:
            raise ValueError(f"class {self.cls!r} is not in the perception ontology")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        x1, y1, x2, y2 = self.bbox
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(f"bbox {self.bbox} must be normalised with x1<x2, y1<y2")


@dataclass(frozen=True)
class ModelInfo:
    """What a model is — travels into every observation as provenance."""

    id: str
    version: str
    task: str = "detect"
    licence: str = "unknown"
    classes: tuple[str, ...] = ()
    input_size: tuple[int, int] | None = None
    backend: str = "python"
    extra: dict[str, Any] = field(default_factory=dict)


class VisionModel(ABC):
    """A detector behind the vendor-isolation boundary."""

    @abstractmethod
    def info(self) -> ModelInfo:
        """Identity, version and licence of the loaded model."""

    @abstractmethod
    def detect(self, frame: Any) -> list[Detection]:
        """Detections for one frame (whatever frame object the source yields)."""
