"""Frame sources for the edge runtime. Needs OpenCV (the ``vision`` extra).

``VideoFileSource`` decodes a recorded clip and **samples by time**, not by frame count, so a 25 fps
8MP stream and a 12 fps substream both yield the configured analysis rate (default 5 fps —
``docs/VISION_ARCHITECTURE.md`` §1: 2–5 fps is plenty for people/zone rules and keeps a CPU box
cool). Frame timestamps are ``start + media time``, so replaying a clip is deterministic and every
observation carries the moment it depicts, not the moment it was processed.

Each yielded :class:`FrameIn` carries the decoded BGR frame (which never leaves this process) and,
when a :class:`QualityMonitor` is supplied, the camera state + quality metrics for health reporting.

Live RTSP (reconnect/backoff, drop-old-not-recent under load) arrives with the Stage C pilot slice.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from minemonitor.vision.edge.quality import QualityMonitor
from minemonitor.vision.edge.runtime import FrameIn


class VideoFileSource:
    """Decode a clip and yield frames at ``sample_fps`` (time-based sampling)."""

    def __init__(
        self,
        path: str | Path,
        *,
        start: datetime,
        sample_fps: float = 5.0,
        quality: QualityMonitor | None = None,
        frame_prefix: str | None = None,
        max_seconds: float | None = None,
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware (UTC)")
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.start = start
        self.sample_fps = sample_fps
        self.quality = quality
        self.frame_prefix = frame_prefix or self.path.stem
        self.max_seconds = max_seconds

    def __iter__(self) -> Iterator[FrameIn]:
        import cv2

        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise OSError(f"cannot decode {self.path}")
        native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        step = 1.0 / self.sample_fps
        next_t = 0.0
        index = 0
        n = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                t = index / native_fps
                index += 1
                if self.max_seconds is not None and t > self.max_seconds:
                    break
                if t + 1e-9 < next_t:
                    continue
                next_t += step
                state, metrics = (None, {})
                if self.quality is not None:
                    state, metrics = self.quality.assess(frame)
                yield FrameIn(
                    ts=self.start + timedelta(seconds=t),
                    frame_id=f"{self.frame_prefix}-{n:06d}",
                    image=frame,
                    camera_state=state,
                    quality=dict(metrics),
                )
                n += 1
        finally:
            cap.release()
