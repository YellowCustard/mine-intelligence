"""Image-space geometry for vision: boxes, foot points, image-zones, homography. Pure Python.

Coordinates are **normalised image coordinates**: ``x`` right, ``y`` down, both in ``[0, 1]``,
independent of resolution — so a scene configured on the 8MP main stream still applies to the
substream the edge actually decodes.

A person or vehicle "is in" an image-zone when its **foot point** (bottom-centre of the box) is
inside the zone polygon — the point that touches the ground, which is what maps onto the site plan.

Homography (only when a camera is calibrated): ``{"matrix": [[h11,h12,h13],[h21,h22,h23],
[h31,h32,h33]]}`` maps a normalised image point ``(x, y)`` to a WGS84 ground point
``(lat, lon)`` via ``[lat·w, lon·w, w] = H · [x, y, 1]``. Without a calibration there is **no**
ground point and no distance claim (``basis: unknown``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

BBox = tuple[float, float, float, float]  # x1, y1, x2, y2 (normalised)
Point = tuple[float, float]


def foot_point(bbox: BBox) -> Point:
    """Bottom-centre of a box — where the object meets the ground."""
    x1, _y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, y2)


def iou(a: BBox, b: BBox) -> float:
    """Intersection-over-union of two boxes (0 when disjoint)."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]]) -> bool:
    """Ray-casting test. Points exactly on an edge may fall either way — debounce absorbs that."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def zones_containing(bbox: BBox, image_zones: Sequence[dict[str, Any]]) -> list[str]:
    """Operational ``zone_id``s whose image-zone polygon contains the box's foot point."""
    fx, fy = foot_point(bbox)
    return [z["zone_id"] for z in image_zones if point_in_polygon(fx, fy, z["polygon"])]


def validate_polygon(polygon: Any) -> list[list[float]]:
    """A normalised polygon: ≥3 ``[x, y]`` points, every coordinate in ``[0, 1]``."""
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise ValueError("polygon needs at least 3 points")
    out: list[list[float]] = []
    for pt in polygon:
        if not (isinstance(pt, list | tuple) and len(pt) == 2):
            raise ValueError("each polygon point must be [x, y]")
        x, y = float(pt[0]), float(pt[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("polygon coordinates must be normalised to [0, 1]")
        out.append([x, y])
    return out


def validate_homography(h: Any) -> list[list[float]]:
    """A 3×3 numeric matrix under ``{"matrix": ...}``."""
    m = h.get("matrix") if isinstance(h, dict) else None
    if not (
        isinstance(m, list) and len(m) == 3 and all(isinstance(r, list) and len(r) == 3 for r in m)
    ):
        raise ValueError("homography must be {'matrix': 3x3}")
    return [[float(v) for v in row] for row in m]


def apply_homography(matrix: Sequence[Sequence[float]], x: float, y: float) -> Point | None:
    """Project a normalised image point to ``(lat, lon)``; ``None`` at the horizon (w≈0)."""
    a = matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]
    b = matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]
    w = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2]
    if abs(w) < 1e-12:
        return None
    lat, lon = a / w, b / w
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)
