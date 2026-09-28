"""Cheap per-frame quality checks → camera state (``vision.camera_health.v1``). Needs NumPy/OpenCV.

Not a model — three classical signals that catch the failures that silently blind a camera
(``docs/VISION_BUILD_GATE.md`` §4.4), so a camera that *sees nothing* is distinguishable from a
zone that *has nothing in it*:

- **luma mean** — too dark (night without IR, lens cap, power to the illuminator lost);
- **Laplacian variance** — too little edge energy: defocus, a dusty/fogged lens, spray;
- **frozen frames** — consecutive near-identical frames: a hung encoder or a stuck stream.

A frame that is both dark *and* edgeless reads as ``obstructed`` (something is covering the lens).
Thresholds are configuration, tuned per camera on commissioning, never hard truths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("frame quality checks need the 'vision' extra") from exc


@dataclass(frozen=True)
class QualityThresholds:
    dark_luma: float = 20.0
    blur_var: float = 15.0
    frozen_diff: float = 0.5
    frozen_frames: int = 25


class QualityMonitor:
    """Stateful per-camera quality assessor (frozen detection needs the previous frame)."""

    def __init__(self, thresholds: QualityThresholds | None = None) -> None:
        self.t = thresholds or QualityThresholds()
        self._prev: Any = None
        self._frozen = 0

    def assess(self, frame_bgr: Any) -> tuple[str, dict[str, float | int]]:
        """Return ``(state, metrics)`` for one BGR frame."""
        import cv2

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        luma = float(small.mean())
        blur = float(cv2.Laplacian(small, cv2.CV_64F).var())
        if self._prev is not None and float(np.abs(small.astype(np.int16) - self._prev).mean()) < (
            self.t.frozen_diff
        ):
            self._frozen += 1
        else:
            self._frozen = 0
        self._prev = small.astype(np.int16)
        metrics: dict[str, float | int] = {
            "luma_mean": round(luma, 2),
            "blur_score": round(blur, 2),
            "frozen_frames": self._frozen,
        }
        dark = luma < self.t.dark_luma
        flat = blur < self.t.blur_var
        if self._frozen >= self.t.frozen_frames:
            state = "no_signal"
        elif dark and flat:
            state = "obstructed"
        elif dark:
            state = "dark"
        elif flat:
            state = "defocused"
        else:
            state = "ok"
        return state, metrics
