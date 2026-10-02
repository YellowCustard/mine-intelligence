"""The vision edge runtime: frame → detect → track → zones → rules → outbox. Stdlib + pure vision.

One :class:`EdgeRuntime` per camera, following ``docs/VISION_ARCHITECTURE.md`` §1:

```
frame ─► model.detect ─► tracker.update ─► image-zones (+ ground point if calibrated)
      ─► vision.observation.v1 (stored locally; sent only in lab mode)
      ─► temporal rules ─► vision.operational_event.v1 ─┐
      ─► rollup ─► vision.zone_rollup.v1 (5-min) ───────┼─► outbox ─► core
      ─► health ─► vision.health.v1 / camera_health.v1 ─┘
```

It runs entirely from the cached ``GET .../vision/config`` document, so it keeps detecting and
alarming locally through a link outage; the outbox holds everything until the core is reachable.
The model is whatever sits behind :class:`VisionModel` — the simulated stub in CI, YOLOX on the
edge box — and nothing here knows which.

Raw frames never leave this process. Only the structured contracts go to the outbox.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from minemonitor.contracts.vision import (
    VisionCameraHealthV1,
    VisionHealthV1,
    VisionObservationV1,
    VisionOperationalEventV1,
    VisionRuleVersionV1,
)
from minemonitor.vision.adapters.base import VisionModel
from minemonitor.vision.edge.evidence import EvidenceStore
from minemonitor.vision.edge.outbox import Outbox
from minemonitor.vision.geometry import apply_homography, foot_point, zones_containing
from minemonitor.vision.rollup import RollupAccumulator
from minemonitor.vision.temporal import TemporalEngine
from minemonitor.vision.tracker import Tracker


@dataclass(frozen=True)
class FrameIn:
    """A decoded frame handed to the runtime. ``image`` is opaque here (the model reads it)."""

    ts: datetime
    frame_id: str
    image: Any
    camera_state: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeStats:
    frames: int = 0
    detections: int = 0
    events: int = 0
    rollups: int = 0
    started: float = field(default_factory=time.monotonic)
    infer_ms_total: float = 0.0

    @property
    def fps(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.frames / elapsed if elapsed > 0 else 0.0

    @property
    def infer_ms_mean(self) -> float:
        return self.infer_ms_total / self.frames if self.frames else 0.0


class EdgeRuntime:
    """Perception + rules for one camera, driven from the cached edge config."""

    def __init__(
        self,
        *,
        site_id: str,
        node_id: str,
        camera_id: str,
        config: dict[str, Any],
        model: VisionModel,
        outbox: Outbox,
        bucket_s: int = 300,
        heartbeat_s: int = 60,
        lab_mode: bool = False,
        tracker: Tracker | None = None,
        evidence: EvidenceStore | None = None,
        node_health: bool = True,
        rollup: RollupAccumulator | None = None,
    ) -> None:
        cam = next((c for c in config.get("cameras", []) if c["camera_id"] == camera_id), None)
        if cam is None:
            raise ValueError(f"camera {camera_id!r} has no scene config in the edge config")
        self.site_id = site_id
        self.node_id = node_id
        self.camera_id = camera_id
        self.model = model
        self.info = model.info()
        self.outbox = outbox
        self.lab_mode = lab_mode
        self.evidence = evidence
        # A multi-camera node reports its own health once; a single-camera runtime reports it here.
        self.node_health = node_health
        self.heartbeat = timedelta(seconds=heartbeat_s)
        self.image_zones: list[dict[str, Any]] = cam.get("image_zones", [])
        homography = cam.get("homography")
        self.homography = homography.get("matrix") if isinstance(homography, dict) else None
        self.calibration_version = cam.get("calibration_version")
        zones: dict[str, dict[str, Any]] = config.get("zones", {})
        rules = [VisionRuleVersionV1.model_validate(r) for r in config.get("rules", [])]
        self.tracker = tracker or Tracker(camera_id)
        self.engine = TemporalEngine(
            site_id=site_id,
            node_id=node_id,
            camera_id=camera_id,
            rules=rules,
            zones=zones,
            zone_versions={z["zone_id"]: z.get("zone_version", "") for z in self.image_zones},
        )
        self.rollup = rollup or RollupAccumulator(
            site_id=site_id,
            node_id=node_id,
            camera_id=camera_id,
            zone_ids=[z["zone_id"] for z in self.image_zones],
            bucket_s=bucket_s,
        )
        self.stats = RuntimeStats()
        self._last_node_health: datetime | None = None
        self._last_cam_health: datetime | None = None
        self._last_cam_state: str | None = None
        self._dropped = 0

    # -- per frame ---------------------------------------------------------------------------

    def process(self, frame: FrameIn) -> list[VisionOperationalEventV1]:
        """Run one frame end to end. Everything produced goes to the outbox first."""
        t0 = time.monotonic()
        detections = self.model.detect(frame.image)
        self.stats.infer_ms_total += (time.monotonic() - t0) * 1000
        tracked = self.tracker.update(detections, frame.ts)
        obs = self._observation(frame, tracked)
        self.outbox.store_observation(obs)
        if self.lab_mode:
            self.outbox.put(obs)
        events = [
            self._with_evidence(ev, frame)
            for ev in self.engine.step(obs, camera_state=frame.camera_state)
        ]
        for ev in events:
            self.outbox.put(ev)
        self.rollup.feed(obs)
        for r in self.rollup.flush(now=frame.ts):
            self.outbox.put(r)
            self.stats.rollups += 1
        self._health(frame)
        self.stats.frames += 1
        self.stats.detections += len(detections)
        self.stats.events += len(events)
        return events

    def idle(self, now: datetime, state: str = "no_signal") -> None:
        """No frame is arriving (stream down): report the camera state and close due buckets.

        Without this a dead stream would simply go quiet; the core would notice only after
        ``MM_VISION_CAMERA_STALE_S``. Reporting ``no_signal`` from the edge is faster and says
        *why*. Rollups keep closing on time, with coverage reflecting the blind period.
        """
        self._camera_health(now, state, {}, last_frame_at=None)
        for r in self.rollup.flush(now=now):
            self.outbox.put(r)
            self.stats.rollups += 1
        if self.node_health:
            self._node_health(now)

    def note_dropped(self, total: int) -> None:
        """Frames the source discarded because the node could not keep up (shed load)."""
        self._dropped = max(self._dropped, total)

    def close(self, now: datetime) -> None:
        """Flush the open rollup bucket and a final heartbeat (clean shutdown)."""
        for r in self.rollup.flush(now=now, force=True):
            self.outbox.put(r)
            self.stats.rollups += 1
        if self.node_health:
            self._node_health(now, force=True)

    def _with_evidence(
        self, ev: VisionOperationalEventV1, frame: FrameIn
    ) -> VisionOperationalEventV1:
        """Keep a still of the confirming frame and point the event at it (never the bytes)."""
        if self.evidence is None:
            return ev
        ref = self.evidence.save(
            camera_id=self.camera_id, event_id=ev.event_id, ts=frame.ts, image=frame.image
        )
        if ref is None:
            return ev
        return ev.model_copy(update={"evidence": ev.evidence.model_copy(update={"clip_ref": ref})})

    # -- builders ----------------------------------------------------------------------------

    def _observation(self, frame: FrameIn, tracked: list[Any]) -> VisionObservationV1:
        objects = []
        for t in tracked:
            ground: dict[str, Any] = {"lat": None, "lon": None, "basis": "unknown"}
            if self.homography is not None:
                gp = apply_homography(self.homography, *foot_point(t.bbox))
                if gp is not None:
                    ground = {"lat": gp[0], "lon": gp[1], "basis": "homography"}
            objects.append(
                {
                    "track_id": t.track_id,
                    "class": t.cls,
                    "confidence": round(t.confidence, 4),
                    "bbox": dict(zip(("x1", "y1", "x2", "y2"), t.bbox, strict=True)),
                    "ground_point": ground,
                    "zone_ids": zones_containing(t.bbox, self.image_zones),
                    "provenance": "observed",
                }
            )
        return VisionObservationV1.model_validate(
            {
                "schema": "vision.observation.v1",
                "observation_id": f"vobs-{self.camera_id}-{frame.frame_id}",
                "site_id": self.site_id,
                "node_id": self.node_id,
                "camera_id": self.camera_id,
                "ts": frame.ts,
                "frame_id": frame.frame_id,
                "model": {"id": self.info.id, "version": self.info.version},
                "calibration_version": self.calibration_version,
                "objects": objects,
                "advisory": True,
            }
        )

    def _health(self, frame: FrameIn) -> None:
        if frame.camera_state is not None:
            self._camera_health(frame.ts, frame.camera_state, frame.quality, last_frame_at=frame.ts)
        if self.node_health:
            self._node_health(frame.ts)

    def _camera_health(
        self,
        now: datetime,
        state: str,
        q: dict[str, Any],
        *,
        last_frame_at: datetime | None,
    ) -> None:
        """Emit camera health on a state change, else once per heartbeat."""
        if (
            state == self._last_cam_state
            and self._last_cam_health is not None
            and now - self._last_cam_health < self.heartbeat
        ):
            return
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        self.outbox.put(
            VisionCameraHealthV1.model_validate(
                {
                    "schema": "vision.camera_health.v1",
                    "health_id": f"vch-{self.camera_id}-{stamp}",
                    "site_id": self.site_id,
                    "node_id": self.node_id,
                    "camera_id": self.camera_id,
                    "ts": now,
                    "state": state,
                    "blur_score": q.get("blur_score"),
                    "luma_mean": q.get("luma_mean"),
                    "frozen_frames": q.get("frozen_frames"),
                    "last_frame_at": last_frame_at,
                    "advisory": True,
                }
            )
        )
        self._last_cam_health = now
        self._last_cam_state = state

    def _node_health(self, now: datetime, *, force: bool = False) -> None:
        if not force and self._last_node_health and now - self._last_node_health < self.heartbeat:
            return
        pending = self.outbox.counts()["pending"]
        self.outbox.put(
            VisionHealthV1.model_validate(
                {
                    "schema": "vision.health.v1",
                    "health_id": f"vh-{self.node_id}-{now.strftime('%Y%m%dT%H%M%S%fZ')}",
                    "site_id": self.site_id,
                    "node_id": self.node_id,
                    "ts": now,
                    "status": "ok",
                    "cameras_active": 1,
                    "fps_mean": round(self.stats.fps, 3),
                    "queue_depth": pending,
                    "dropped_frames": self._dropped,
                    "model_ids": [f"{self.info.id}@{self.info.version}"],
                    "advisory": True,
                }
            )
        )
        self._last_node_health = now
