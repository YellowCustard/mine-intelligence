"""Small WGS84 helpers for the simulator. Coordinates are lat/lon, in that order.

These use an equirectangular approximation, which is fine at mine scale (a few km)
and keeps the simulator dependency-free. Anything needing real geodesy belongs in
a projected library, documented explicitly (brief §12).
"""

from __future__ import annotations

import math

# Metres per degree of latitude (roughly constant).
_M_PER_DEG_LAT = 111_320.0


def metres_per_deg_lon(lat_deg: float) -> float:
    """Metres per degree of longitude at a given latitude."""
    return _M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, in degrees (0=N, clockwise)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_m(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Return a new (lat, lon) offset from a point by metres north and east."""
    dlat = north_m / _M_PER_DEG_LAT
    dlon = east_m / metres_per_deg_lon(lat)
    return lat + dlat, lon + dlon
