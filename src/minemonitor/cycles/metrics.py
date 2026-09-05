"""Per-asset ``asset.metrics.v1`` rollups over fixed time buckets.

Derived from positions, recomputable, never hand-edited (brief §6). Each segment
between consecutive fixes is attributed to the bucket of its earlier fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from minemonitor.cycles.statemachine import MOVE_THRESHOLD_KPH, Cycle, Fix
from minemonitor.ingest.geo import haversine_m
from minemonitor.zones.geometry import point_in_polygon

BUCKET_S = 300  # 5-minute buckets (brief §6)


@dataclass
class MetricsBucket:
    site_id: str
    asset_id: str
    bucket_start: datetime
    bucket_end: datetime
    distance_m: float = 0.0
    moving_time_s: float = 0.0
    idle_time_s: float = 0.0
    max_speed_kph: float = 0.0
    _speed_sum: float = 0.0
    _speed_n: int = 0
    zone_dwell_s: dict[str, float] = field(default_factory=dict)
    loads_completed: int = 0

    @property
    def mean_speed_kph(self) -> float:
        return self._speed_sum / self._speed_n if self._speed_n else 0.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _floor_bucket(ts: datetime, bucket_s: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % bucket_s), tz=UTC)


def compute_metrics(
    site_id: str,
    asset_id: str,
    fixes: list[Fix],
    *,
    zones: list[tuple[str, dict]] | None = None,
    cycles: list[Cycle] | None = None,
    bucket_s: int = BUCKET_S,
) -> list[MetricsBucket]:
    """Roll a position stream up into fixed-size metric buckets."""
    fixes = sorted(fixes, key=lambda f: _aware(f.ts))
    if not fixes:
        return []
    zones = zones or []
    buckets: dict[datetime, MetricsBucket] = {}

    def bucket_for(ts: datetime) -> MetricsBucket:
        start = _floor_bucket(ts, bucket_s)
        b = buckets.get(start)
        if b is None:
            b = MetricsBucket(
                site_id=site_id,
                asset_id=asset_id,
                bucket_start=start,
                bucket_end=start + timedelta(seconds=bucket_s),
            )
            buckets[start] = b
        return b

    for i, f in enumerate(fixes):
        ts = _aware(f.ts)
        b = bucket_for(ts)
        speed = f.speed_kph or 0.0
        b.max_speed_kph = max(b.max_speed_kph, speed)
        b._speed_sum += speed
        b._speed_n += 1
        # Attribute the interval to the *next* fix to the earlier fix's bucket.
        if i + 1 < len(fixes):
            dt = (_aware(fixes[i + 1].ts) - ts).total_seconds()
            if 0 < dt <= bucket_s:  # ignore large gaps (offline stretches)
                if speed >= MOVE_THRESHOLD_KPH:
                    b.moving_time_s += dt
                    b.distance_m += haversine_m(f.lat, f.lon, fixes[i + 1].lat, fixes[i + 1].lon)
                else:
                    b.idle_time_s += dt
                for zone_id, geom in zones:
                    if point_in_polygon(f.lat, f.lon, geom):
                        b.zone_dwell_s[zone_id] = b.zone_dwell_s.get(zone_id, 0.0) + dt

    for cyc in cycles or []:
        buckets_key = _floor_bucket(_aware(cyc.end_ts), bucket_s)
        b = buckets.get(buckets_key)
        if b is not None:
            b.loads_completed += 1

    return [buckets[k] for k in sorted(buckets)]
