"""Unit tests for per-bucket metric rollups and shift windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from minemonitor.cycles.metrics import compute_metrics
from minemonitor.cycles.shifts import shift_bounds
from minemonitor.ingest.geo import offset_m
from minemonitor.zones.geometry import box_polygon

_T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


@dataclass
class _Fix:
    ts: datetime
    lat: float
    lon: float
    speed_kph: float | None


def test_buckets_split_on_five_minutes() -> None:
    # 8 minutes of fixes at 1 Hz spans two 5-minute buckets.
    fixes = [_Fix(_T0 + timedelta(seconds=i), -17.8, 31.0, 0.0) for i in range(8 * 60)]
    buckets = compute_metrics("s", "a", fixes)
    assert len(buckets) == 2


def test_idle_time_accumulates_when_stationary() -> None:
    fixes = [_Fix(_T0 + timedelta(seconds=i), -17.8, 31.0, 0.0) for i in range(60)]
    b = compute_metrics("s", "a", fixes)[0]
    assert b.idle_time_s >= 58  # ~59 one-second intervals, all idle
    assert b.moving_time_s == 0.0
    assert b.distance_m == 0.0


def test_moving_time_and_distance() -> None:
    # Move ~10 m east each second (=> 36 kph) for 60 s.
    fixes = []
    lat, lon = -17.8, 31.0
    for i in range(60):
        fixes.append(_Fix(_T0 + timedelta(seconds=i), lat, lon, 36.0))
        lat, lon = offset_m(lat, lon, 0, 10)
    b = compute_metrics("s", "a", fixes)[0]
    assert b.moving_time_s >= 58
    assert b.idle_time_s == 0.0
    assert 550 < b.distance_m < 610  # ~59 * 10 m
    assert b.max_speed_kph == 36.0


def test_zone_dwell_accumulates() -> None:
    zone = box_polygon(-17.8, 31.0, 50)
    fixes = [_Fix(_T0 + timedelta(seconds=i), -17.8, 31.0, 0.0) for i in range(60)]
    b = compute_metrics("s", "a", fixes, zones=[("z1", zone)])[0]
    assert b.zone_dwell_s["z1"] >= 58


def test_fixes_without_speed_are_neither_moving_nor_idle() -> None:
    """A source without speed must not show up as an idle machine (audit M10)."""
    fixes = [_Fix(_T0 + timedelta(seconds=i), -17.8, 31.0, None) for i in range(60)]
    fixes += [_Fix(_T0 + timedelta(seconds=60 + i), -17.8, 31.0, 0.0) for i in range(11)]
    b = compute_metrics("s", "a", fixes)[0]
    assert b.moving_time_s == 0.0
    assert b.idle_time_s == 10.0  # only the intervals that start at a known 0 kph
    assert b.mean_speed_kph == 0.0 and b.max_speed_kph == 0.0


def test_mean_speed_ignores_fixes_without_speed() -> None:
    fixes = [
        _Fix(_T0, -17.8, 31.0, 20.0),
        _Fix(_T0 + timedelta(seconds=1), -17.8, 31.0, None),
        _Fix(_T0 + timedelta(seconds=2), -17.8, 31.0, 40.0),
    ]
    b = compute_metrics("s", "a", fixes)[0]
    assert b.mean_speed_kph == 30.0
    assert b.max_speed_kph == 40.0


def test_shift_bounds_day_and_night_cross_midnight() -> None:
    d = date(2026, 9, 5)
    day_start, day_end = shift_bounds(d, "day")  # 06:00–18:00 Harare (UTC+2)
    assert day_start == datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
    assert day_end == datetime(2026, 9, 5, 16, 0, tzinfo=UTC)

    night_start, night_end = shift_bounds(d, "night")  # 18:00–06:00 next day
    assert night_start == datetime(2026, 9, 5, 16, 0, tzinfo=UTC)
    assert night_end == datetime(2026, 9, 6, 4, 0, tzinfo=UTC)  # crosses midnight
