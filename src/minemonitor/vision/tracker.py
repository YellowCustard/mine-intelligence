"""Multi-object tracker — ByteTrack-style, implemented in-house. Pure Python, deterministic.

Gives each object a **stable per-camera ``track_id``** across frames. That id is temporal identity
*within one camera*; it is never an asset id and never a person's identity (``CLAUDE.md`` §4).

The algorithm follows ByteTrack (Zhang et al., 2022 — the published method, not any packaged
implementation; no Ultralytics/AGPL code, no ``cython_bbox``/``lap`` dependency):

1. **Predict** every live track forward with a constant-velocity filter (α-β on the box centre).
   A track is matched on the *better* of its predicted box and its last observed box, so a person
   who stops dead (or starts moving) is not lost to an overshooting prediction; velocity decays
   while a track coasts unobserved.
2. **Stage 1:** associate *high*-confidence detections to tracks by IoU (same class only).
   The IoU floor is ByteTrack's (match cost 0.8 ⇒ IoU ≥ 0.2).
3. **Stage 2:** associate the *low*-confidence detections to the tracks still unmatched — this is
   ByteTrack's key idea: a partly occluded person scores low but is still the same person, so it
   keeps its id instead of spawning a new one.
4. Unmatched high-confidence detections start **tentative** tracks, confirmed after ``min_hits``.
   Tentative tracks that miss a frame are dropped (kills single-frame false positives).
5. Confirmed tracks survive up to ``max_missed`` missed frames (occlusion, dust) before deletion.

Association is greedy by descending IoU — at mine scene densities (tens of objects) it matches the
Hungarian result in practice, keeps this dependency-free, and is fully reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from minemonitor.vision.adapters.base import Detection
from minemonitor.vision.geometry import BBox, iou


@dataclass(frozen=True)
class TrackedObject:
    """A confirmed track observed in this frame (the detection's own box and confidence)."""

    track_id: str
    cls: str
    confidence: float
    bbox: BBox


@dataclass
class _Track:
    num: int
    cls: str
    cx: float
    cy: float
    w: float
    h: float
    vx: float = 0.0
    vy: float = 0.0
    last_box: BBox = (0.0, 0.0, 0.0, 0.0)
    hits: int = 1
    missed: int = 0
    confirmed: bool = False
    last_ts: datetime | None = None

    def predicted(self, ts: datetime) -> BBox:
        dt = (ts - self.last_ts).total_seconds() if self.last_ts else 0.0
        cx, cy = self.cx + self.vx * dt, self.cy + self.vy * dt
        return (cx - self.w / 2, cy - self.h / 2, cx + self.w / 2, cy + self.h / 2)


class Tracker:
    """ByteTrack-style tracker for one camera."""

    def __init__(
        self,
        camera_id: str,
        *,
        high_thresh: float = 0.5,
        low_thresh: float = 0.1,
        match_iou: float = 0.2,
        min_hits: int = 2,
        max_missed: int = 15,
        alpha: float = 0.6,
        beta: float = 0.2,
        coast_decay: float = 0.5,
    ) -> None:
        self.camera_id = camera_id
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_iou = match_iou
        self.min_hits = min_hits
        self.max_missed = max_missed
        self.alpha = alpha
        self.beta = beta
        self.coast_decay = coast_decay
        self._tracks: list[_Track] = []
        self._next = 1

    def track_id(self, num: int) -> str:
        return f"{self.camera_id}:{num}"

    @property
    def live_tracks(self) -> int:
        return len(self._tracks)

    def update(self, detections: list[Detection], ts: datetime) -> list[TrackedObject]:
        """Advance one frame; return the confirmed tracks seen in it."""
        high = [d for d in detections if d.confidence >= self.high_thresh]
        low = [d for d in detections if self.low_thresh <= d.confidence < self.high_thresh]
        preds = {id(t): (t.predicted(ts), t.last_box) for t in self._tracks}

        matched: list[tuple[_Track, Detection]] = []
        unmatched_tracks = list(self._tracks)
        unmatched_high = self._associate(high, unmatched_tracks, preds, matched)
        # Stage 2: low-confidence detections only rescue already-confirmed tracks.
        confirmed_left = [t for t in unmatched_tracks if t.confirmed]
        self._associate(low, confirmed_left, preds, matched)
        matched_ids = {id(t) for t, _ in matched}

        out: list[TrackedObject] = []
        for t, d in matched:
            self._correct(t, d, ts)
            if t.confirmed:
                out.append(TrackedObject(self.track_id(t.num), t.cls, d.confidence, d.bbox))

        survivors: list[_Track] = []
        for t in self._tracks:
            if id(t) in matched_ids:
                survivors.append(t)
                continue
            t.missed += 1
            t.vx *= self.coast_decay
            t.vy *= self.coast_decay
            if t.confirmed and t.missed <= self.max_missed:
                survivors.append(t)  # coast through occlusion
            # tentative tracks that miss a frame are dropped (single-frame blips)
        for d in unmatched_high:
            survivors.append(self._new(d, ts))
        self._tracks = survivors
        out.sort(key=lambda o: o.track_id)
        return out

    def _associate(
        self,
        dets: list[Detection],
        tracks: list[_Track],
        preds: dict[int, tuple[BBox, BBox]],
        matched: list[tuple[_Track, Detection]],
    ) -> list[Detection]:
        """Greedy IoU matching (same class). Mutates ``tracks`` to the unmatched remainder."""
        pairs = sorted(
            (
                (max(iou(preds[id(t)][0], d.bbox), iou(preds[id(t)][1], d.bbox)), ti, di)
                for ti, t in enumerate(tracks)
                for di, d in enumerate(dets)
                if t.cls == d.cls
            ),
            key=lambda p: (-p[0], p[1], p[2]),
        )
        used_t: set[int] = set()
        used_d: set[int] = set()
        for score, ti, di in pairs:
            if score < self.match_iou:
                break
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            matched.append((tracks[ti], dets[di]))
        remaining = [t for i, t in enumerate(tracks) if i not in used_t]
        tracks[:] = remaining
        return [d for i, d in enumerate(dets) if i not in used_d]

    def _new(self, d: Detection, ts: datetime) -> _Track:
        x1, y1, x2, y2 = d.bbox
        t = _Track(
            num=self._next,
            cls=d.cls,
            cx=(x1 + x2) / 2,
            cy=(y1 + y2) / 2,
            w=x2 - x1,
            h=y2 - y1,
            last_box=d.bbox,
            last_ts=ts,
            confirmed=self.min_hits <= 1,
        )
        self._next += 1
        return t

    def _correct(self, t: _Track, d: Detection, ts: datetime) -> None:
        dt = (ts - t.last_ts).total_seconds() if t.last_ts else 0.0
        x1, y1, x2, y2 = d.bbox
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        px, py = t.cx + t.vx * dt, t.cy + t.vy * dt
        rx, ry = mx - px, my - py
        t.cx, t.cy = px + self.alpha * rx, py + self.alpha * ry
        if dt > 0:
            t.vx += self.beta * rx / dt
            t.vy += self.beta * ry / dt
        t.w, t.h = x2 - x1, y2 - y1
        t.last_box = d.bbox
        t.hits += 1
        t.missed = 0
        t.last_ts = ts
        if t.hits >= self.min_hits:
            t.confirmed = True
