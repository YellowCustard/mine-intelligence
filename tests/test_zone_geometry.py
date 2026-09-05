"""Unit tests for point-in-polygon and distance-to-polygon."""

from __future__ import annotations

from minemonitor.zones.geometry import (
    box_polygon,
    distance_to_polygon_m,
    point_in_polygon,
)

# A ~100 m box (half 50 m) near the placeholder site.
_C_LAT, _C_LON = -17.8252, 31.0335
_BOX = box_polygon(_C_LAT, _C_LON, 50)


def test_center_is_inside() -> None:
    assert point_in_polygon(_C_LAT, _C_LON, _BOX) is True


def test_far_point_is_outside() -> None:
    assert point_in_polygon(_C_LAT + 0.01, _C_LON, _BOX) is False


def test_distance_zero_when_inside() -> None:
    assert distance_to_polygon_m(_C_LAT, _C_LON, _BOX) == 0.0


def test_distance_matches_offset() -> None:
    # ~100 m north of centre is ~50 m outside a 50 m half-box.
    lat_100m_north = _C_LAT + 100.0 / 111_320.0
    dist = distance_to_polygon_m(lat_100m_north, _C_LON, _BOX)
    assert 45 < dist < 55


def test_point_just_inside_boundary() -> None:
    # 40 m north of centre is still inside a 50 m half-box.
    lat = _C_LAT + 40.0 / 111_320.0
    assert point_in_polygon(lat, _C_LON, _BOX) is True
