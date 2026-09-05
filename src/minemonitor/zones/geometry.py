"""Point-in-polygon and point-to-polygon distance for WGS84 zone polygons.

Polygons are GeoJSON rings: ``[[lon, lat], ...]`` (GeoJSON is lon-first). Distance
uses a local equirectangular projection centred on the query point, which is
accurate to well under a metre at mine scale and keeps us free of PostGIS
(brief §7: one database, boring tech). Any projected use is documented here.
"""

from __future__ import annotations

import math

_M_PER_DEG_LAT = 111_320.0

# A GeoJSON polygon ring as (lon, lat) pairs.
Ring = list[list[float]]


def _outer_ring(geometry: dict) -> Ring:
    """Extract the outer ring from a GeoJSON Polygon."""
    coords = geometry["coordinates"]
    return coords[0]


def point_in_polygon(lat: float, lon: float, geometry: dict) -> bool:
    """Ray-casting test: is (lat, lon) inside the polygon's outer ring?"""
    ring = _outer_ring(geometry)
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]  # lon, lat
        xj, yj = ring[j][0], ring[j][1]
        # Does the horizontal ray at `lat` cross edge (i, j)?
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) / (yj - yi) * (xj - xi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _seg_distance_m(
    px: float, py: float, ax: float, ay: float, bx: float, by: float, m_per_deg_lon: float
) -> float:
    """Distance (m) from point P to segment AB, all in degrees, via local metres."""
    # Project degrees to metres in a local tangent plane.
    pxm, pym = px * m_per_deg_lon, py * _M_PER_DEG_LAT
    axm, aym = ax * m_per_deg_lon, ay * _M_PER_DEG_LAT
    bxm, bym = bx * m_per_deg_lon, by * _M_PER_DEG_LAT
    dx, dy = bxm - axm, bym - aym
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        return math.hypot(pxm - axm, pym - aym)
    t = max(0.0, min(1.0, ((pxm - axm) * dx + (pym - aym) * dy) / seg_len_sq))
    cx, cy = axm + t * dx, aym + t * dy
    return math.hypot(pxm - cx, pym - cy)


def distance_to_polygon_m(lat: float, lon: float, geometry: dict) -> float:
    """Shortest distance in metres from (lat, lon) to the polygon boundary.

    Returns 0.0 when the point is inside. Used for exit hysteresis: an exit is
    confirmed only once the point is this many metres *outside* the boundary.
    """
    if point_in_polygon(lat, lon, geometry):
        return 0.0
    ring = _outer_ring(geometry)
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    best = math.inf
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i][0], ring[i][1]
        bx, by = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        best = min(best, _seg_distance_m(lon, lat, ax, ay, bx, by, m_per_deg_lon))
    return best


def box_polygon(center_lat: float, center_lon: float, half_m: float) -> dict:
    """A square GeoJSON polygon centred on a point, half-width ``half_m`` metres.

    Convenience for seeds and tests — real zones come from the site survey.
    """
    dlat = half_m / _M_PER_DEG_LAT
    dlon = half_m / (_M_PER_DEG_LAT * math.cos(math.radians(center_lat)))
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [center_lon - dlon, center_lat - dlat],
                [center_lon + dlon, center_lat - dlat],
                [center_lon + dlon, center_lat + dlat],
                [center_lon - dlon, center_lat + dlat],
                [center_lon - dlon, center_lat - dlat],
            ]
        ],
    }
