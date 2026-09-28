"""Per camera × zone × time-bucket people counts → ``vision.zone_rollup.v1``. Pure Python.

The periodic rollups of ``docs/VISION_BUILD_GATE.md`` §5: instead of streaming every frame over
the thin mine link, the edge sends one small record per zone per bucket (default 5 min) — count
peak/mean, entries/exits, dwell — which is what reporting and the dashboard read.

- ``zone_id = None`` is the whole camera view.
- **Coverage** (``observed_s``) is the time the camera actually produced frames in the bucket (frame
  gaps longer than ``max_gap_s`` are not counted), so an empty zone is distinguishable from a blind
  camera — the "missed" discipline applied to analytics.
- Entries/exits are debounced (``confirm_frames`` consecutive frames in/out), so a box flickering on
  an edge does not inflate the count.
- Counts only. No identity, ever: a track id is per-camera temporal identity and never leaves the
  edge in a rollup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from minemonitor.contracts.vision import VisionObservationV1, VisionZoneRollupV1


@dataclass
class _Bucket:
    start: datetime
    samples: dict[str | None, list[int]] = field(default_factory=dict)
    entries: dict[str | None, int] = field(default_factory=dict)
    exits: dict[str | None, int] = field(default_factory=dict)
    dwells: dict[str | None, list[float]] = field(default_factory=dict)
    observed_s: float = 0.0


@dataclass
class _Visit:
    inside: bool = False
    streak_in: int = 0
    streak_out: int = 0
    start: datetime | None = None
    last_in: datetime | None = None


class RollupAccumulator:
    """Accumulate one camera's observations into fixed time buckets."""

    def __init__(
        self,
        *,
        site_id: str,
        node_id: str,
        camera_id: str,
        zone_ids: list[str],
        bucket_s: int = 300,
        object_class: str = "person",
        max_gap_s: float = 2.0,
        confirm_frames: int = 2,
    ) -> None:
        self.site_id = site_id
        self.node_id = node_id
        self.camera_id = camera_id
        self.zone_keys: list[str | None] = [None, *zone_ids]
        self.bucket_s = bucket_s
        self.object_class = object_class
        self.max_gap_s = max_gap_s
        self.confirm_frames = confirm_frames
        self._bucket: _Bucket | None = None
        self._done: list[VisionZoneRollupV1] = []
        self._last_ts: datetime | None = None
        self._visits: dict[tuple[str | None, str], _Visit] = {}

    def _bucket_start(self, ts: datetime) -> datetime:
        epoch = int(ts.timestamp())
        return datetime.fromtimestamp(epoch - epoch % self.bucket_s, tz=UTC)

    def feed(self, obs: VisionObservationV1) -> None:
        start = self._bucket_start(obs.ts)
        if self._bucket is None or start != self._bucket.start:
            if self._bucket is not None:
                self._close(self._bucket)
            self._bucket = _Bucket(start=start)
        b = self._bucket
        if self._last_ts is not None and self._bucket_start(self._last_ts) == start:
            b.observed_s += min(max((obs.ts - self._last_ts).total_seconds(), 0.0), self.max_gap_s)
        self._last_ts = obs.ts

        members: dict[str | None, set[str]] = {k: set() for k in self.zone_keys}
        for o in obs.objects:
            if o.class_ != self.object_class:
                continue
            members[None].add(o.track_id)
            for z in o.zone_ids:
                if z in members:
                    members[z].add(o.track_id)
        for key in self.zone_keys:
            b.samples.setdefault(key, []).append(len(members[key]))
            self._visit_step(b, key, members[key], obs.ts)

    def _visit_step(self, b: _Bucket, key: str | None, present: set[str], ts: datetime) -> None:
        known = {t for (k, t) in self._visits if k == key}
        for track in present | known:
            v = self._visits.setdefault((key, track), _Visit())
            if track in present:
                v.streak_out = 0
                v.streak_in += 1
                v.last_in = ts
                if not v.inside and v.streak_in >= self.confirm_frames:
                    v.inside = True
                    v.start = ts
                    b.entries[key] = b.entries.get(key, 0) + 1
            else:
                v.streak_in = 0
                v.streak_out += 1
                if v.inside and v.streak_out >= self.confirm_frames:
                    b.exits[key] = b.exits.get(key, 0) + 1
                    if v.start and v.last_in:
                        b.dwells.setdefault(key, []).append((v.last_in - v.start).total_seconds())
                    del self._visits[(key, track)]
                elif not v.inside and v.streak_out >= self.confirm_frames:
                    del self._visits[(key, track)]

    def _close(self, b: _Bucket) -> None:
        for key in self.zone_keys:
            samples = b.samples.get(key, [])
            if not samples:
                continue
            done = sorted(b.dwells.get(key, []))
            ongoing = [
                (v.last_in - v.start).total_seconds()
                for (k, _t), v in self._visits.items()
                if k == key and v.inside and v.start and v.last_in
            ]
            dwell_max = max([*done, *ongoing], default=0.0)
            p50 = done[len(done) // 2] if done else None
            self._done.append(
                VisionZoneRollupV1.model_validate(
                    {
                        "schema": "vision.zone_rollup.v1",
                        "rollup_id": f"vr-{self.camera_id}-{key or 'view'}-"
                        f"{b.start.strftime('%Y%m%dT%H%M%SZ')}-{self.object_class}",
                        "site_id": self.site_id,
                        "node_id": self.node_id,
                        "camera_id": self.camera_id,
                        "zone_id": key,
                        "ts": b.start,
                        "bucket_s": self.bucket_s,
                        "object_class": self.object_class,
                        "count_max": max(samples),
                        "count_mean": round(sum(samples) / len(samples), 3),
                        "entries": b.entries.get(key, 0),
                        "exits": b.exits.get(key, 0),
                        "dwell_s_max": round(dwell_max, 3),
                        "dwell_s_p50": round(p50, 3) if p50 is not None else None,
                        "observed_s": round(min(b.observed_s, float(self.bucket_s)), 3),
                        "advisory": True,
                    }
                )
            )

    def flush(
        self, *, now: datetime | None = None, force: bool = False
    ) -> list[VisionZoneRollupV1]:
        """Return completed buckets. ``force`` also closes the open bucket (shutdown)."""
        if self._bucket is not None:
            ended = now is not None and now >= self._bucket.start + timedelta(seconds=self.bucket_s)
            if force or ended:
                self._close(self._bucket)
                self._bucket = None
        out, self._done = self._done, []
        return out
