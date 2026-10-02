"""Spatial scene context and reporting for first-party vision (V4). Core side; callers commit.

Scene context here is deliberately **spatial and deterministic** (the 2026-09-28 gate decision):
which operational zones each camera sees, how many people the edge counted in each, whether the
camera can currently see at all, and where a calibrated camera's ground plane sits. No captioning,
no "understanding" a model cannot evidence — every number below traces to a stored
``vision.zone_rollup.v1`` / ``vision.camera_health.v1`` / ``vision.operational_event.v1`` row.

- :func:`set_scene` validates and stores a camera's image-zones (normalised polygons → operational
  ``zone_id``) and optional ground-plane homography. A homography marks the camera ``calibrated``;
  without one there is no ground point and no distance claim.
- :func:`scene_state` — per camera: latest counts per zone (from the 5-min rollups, labelled as
  such, with staleness), current camera condition, open vision alarms.
- :func:`vision_report` — a shift/window report per camera × zone: peak and time-weighted mean
  people, entries/exits, longest dwell, restricted intrusions, and **coverage** — the share of the
  window the camera was actually producing frames, so "nobody there" is never confused with
  "nobody watching". Observed numbers only; no targets or benchmarks.

Counts, never identities.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.storage.models import (
    Camera,
    Event,
    VisionCameraHealth,
    VisionOperationalEvent,
    VisionZoneRollup,
    Zone,
)
from minemonitor.vision.geometry import validate_homography, validate_polygon

MAX_IMAGE_ZONES = 16
INTRUSION_TYPES = ("person_in_restricted_zone", "vehicle_in_restricted_zone")


class SceneError(ValueError):
    """A scene configuration the platform refuses."""


def _utc(ts: datetime) -> datetime:
    """SQLite drops tzinfo on read; stored datetimes are UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Scene configuration
# ---------------------------------------------------------------------------------------------


def set_scene(
    session: Session,
    site_id: str,
    camera_id: str,
    *,
    image_zones: list[dict[str, Any]],
    homography: dict[str, Any] | None = None,
    update_homography: bool = False,
    now: datetime | None = None,
) -> Camera:
    """Validate and store a camera's scene. Raises :class:`SceneError` / ``LookupError``."""
    cam = session.get(Camera, camera_id)
    if cam is None or cam.site_id != site_id:
        raise LookupError(f"camera {camera_id!r} not found for site {site_id!r}")
    if len(image_zones) > MAX_IMAGE_ZONES:
        raise SceneError(f"at most {MAX_IMAGE_ZONES} image-zones per camera")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, z in enumerate(image_zones):
        if not isinstance(z, dict):
            raise SceneError(f"image_zones[{i}] must be an object")
        zone_id = z.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id:
            raise SceneError(f"image_zones[{i}].zone_id is required")
        if zone_id in seen:
            raise SceneError(f"zone {zone_id!r} mapped twice on one camera")
        seen.add(zone_id)
        if session.get(Zone, {"zone_id": zone_id, "site_id": site_id}) is None:
            raise SceneError(f"zone {zone_id!r} does not exist for site {site_id!r}")
        version = z.get("zone_version")
        if not isinstance(version, str) or not version:
            raise SceneError(f"image_zones[{i}].zone_version is required (e.g. a survey date)")
        try:
            polygon = validate_polygon(z.get("polygon"))
        except (TypeError, ValueError) as exc:
            raise SceneError(f"image_zones[{i}].polygon: {exc}") from exc
        cleaned.append({"zone_id": zone_id, "zone_version": version, "polygon": polygon})
    cam.image_zones = cleaned
    if update_homography:
        if homography is None:
            cam.homography = None
            cam.calibration_status = "none"
        else:
            try:
                matrix = validate_homography(homography)
            except (TypeError, ValueError) as exc:
                raise SceneError(f"homography: {exc}") from exc
            cam.homography = {"matrix": matrix}
            cam.calibration_status = "calibrated"
    cam.updated_at = now or datetime.now(UTC)
    return cam


def scene_config(cam: Camera) -> dict[str, Any]:
    return {
        "camera_id": cam.id,
        "name": cam.name,
        "image_zones": cam.image_zones or [],
        "calibration_status": cam.calibration_status,
        "homography": cam.homography,
    }


# ---------------------------------------------------------------------------------------------
# Live scene state
# ---------------------------------------------------------------------------------------------


def _latest_rollups(
    session: Session, site_id: str, camera_id: str
) -> dict[str | None, VisionZoneRollup]:
    """Latest person rollup per zone (``None`` = whole view) for one camera."""
    latest_ts = (
        session.execute(
            select(func.max(VisionZoneRollup.ts)).where(
                VisionZoneRollup.site_id == site_id,
                VisionZoneRollup.camera_id == camera_id,
                VisionZoneRollup.object_class == "person",
            )
        )
        .scalars()
        .first()
    )
    if latest_ts is None:
        return {}
    rows = session.execute(
        select(VisionZoneRollup).where(
            VisionZoneRollup.site_id == site_id,
            VisionZoneRollup.camera_id == camera_id,
            VisionZoneRollup.object_class == "person",
            VisionZoneRollup.ts == latest_ts,
        )
    ).scalars()
    return {r.zone_id: r for r in rows}


def scene_state(session: Session, site_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Per configured camera: what it sees now (latest rollup), its condition, open alarms."""
    now = now or datetime.now(UTC)
    cams = (
        session.execute(
            select(Camera)
            .where(Camera.site_id == site_id, Camera.enabled.is_(True))
            .order_by(Camera.name)
        )
        .scalars()
        .all()
    )
    zones = {
        z.zone_id: z for z in session.execute(select(Zone).where(Zone.site_id == site_id)).scalars()
    }
    out = []
    for cam in cams:
        if not cam.image_zones:
            continue
        health = (
            session.execute(
                select(VisionCameraHealth)
                .where(
                    VisionCameraHealth.site_id == site_id,
                    VisionCameraHealth.camera_id == cam.id,
                )
                .order_by(VisionCameraHealth.ts.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        rollups = _latest_rollups(session, site_id, cam.id)
        view = rollups.get(None)
        stale = True
        if view is not None:
            bucket_end = _utc(view.ts) + timedelta(seconds=view.bucket_s)
            stale = now - bucket_end > timedelta(seconds=2 * view.bucket_s)
        open_alarms = (
            session.execute(
                select(func.count(Event.event_id)).where(
                    Event.site_id == site_id,
                    Event.source == f"vision:{cam.id}",
                    Event.state != "resolved",
                )
            )
            .scalars()
            .one()
        )
        zone_rows = []
        for iz in cam.image_zones:
            zid = iz["zone_id"]
            r = rollups.get(zid)
            z = zones.get(zid)
            zone_rows.append(
                {
                    "zone_id": zid,
                    "name": z.name if z else zid,
                    "kind": z.kind if z else None,
                    "people_peak": r.count_max if r else None,
                    "people_mean": r.count_mean if r else None,
                    "entries": r.entries if r else None,
                }
            )
        out.append(
            {
                "camera_id": cam.id,
                "name": cam.name,
                "calibration_status": cam.calibration_status,
                "condition": health.state if health else "unknown",
                "condition_at": _aware(health.ts) if health else None,
                "health_state": cam.health_state,
                "people_in_view_peak": view.count_max if view else None,
                "bucket_start": _aware(view.ts) if view else None,
                "bucket_s": view.bucket_s if view else None,
                "coverage_s": view.observed_s if view else None,
                "stale": stale,
                "open_alarms": int(open_alarms),
                "zones": zone_rows,
                "provenance": "observed",
                "basis": "latest 5-min rollup from the edge node",
            }
        )
    return {"site_id": site_id, "generated_at": now, "cameras": out}


# ---------------------------------------------------------------------------------------------
# Window / shift report
# ---------------------------------------------------------------------------------------------


def vision_report(session: Session, site_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Per camera × zone people statistics over ``[start, end)``. Observed numbers only."""
    if end <= start:
        raise ValueError("report window end must be after start")
    window_s = (end - start).total_seconds()
    rollups = (
        session.execute(
            select(VisionZoneRollup).where(
                VisionZoneRollup.site_id == site_id,
                VisionZoneRollup.object_class == "person",
                VisionZoneRollup.ts >= start,
                VisionZoneRollup.ts < end,
            )
        )
        .scalars()
        .all()
    )
    intrusions = session.execute(
        select(
            VisionOperationalEvent.camera_id,
            VisionOperationalEvent.zone_id,
            func.count(VisionOperationalEvent.event_id),
        )
        .where(
            VisionOperationalEvent.site_id == site_id,
            VisionOperationalEvent.type.in_(INTRUSION_TYPES),
            VisionOperationalEvent.promoted_event_id.is_not(None),
            VisionOperationalEvent.ts >= start,
            VisionOperationalEvent.ts < end,
        )
        .group_by(VisionOperationalEvent.camera_id, VisionOperationalEvent.zone_id)
    ).all()
    intr = {(c, z): int(n) for c, z, n in intrusions}
    cams = {
        c.id: c.name
        for c in session.execute(select(Camera).where(Camera.site_id == site_id)).scalars()
    }
    zones = {
        z.zone_id: z.name
        for z in session.execute(select(Zone).where(Zone.site_id == site_id)).scalars()
    }
    acc: dict[tuple[str, str | None], dict[str, Any]] = {}
    for r in rollups:
        a = acc.setdefault(
            (r.camera_id, r.zone_id),
            {
                "peak": 0,
                "weighted": 0.0,
                "observed_s": 0.0,
                "entries": 0,
                "exits": 0,
                "dwell_s_max": 0.0,
                "buckets": 0,
            },
        )
        a["peak"] = max(a["peak"], r.count_max)
        a["weighted"] += r.count_mean * r.observed_s
        a["observed_s"] += r.observed_s
        a["entries"] += r.entries
        a["exits"] += r.exits
        a["dwell_s_max"] = max(a["dwell_s_max"], r.dwell_s_max)
        a["buckets"] += 1
    rows = []
    for (cam_id, zone_id), a in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        view = acc.get((cam_id, None))
        coverage_s = view["observed_s"] if view else a["observed_s"]
        rows.append(
            {
                "camera_id": cam_id,
                "camera_name": cams.get(cam_id, cam_id),
                "zone_id": zone_id,
                "zone_name": zones.get(zone_id, zone_id) if zone_id else "(whole view)",
                "people_peak": a["peak"],
                "people_mean": round(a["weighted"] / a["observed_s"], 3)
                if a["observed_s"]
                else None,
                "entries": a["entries"],
                "exits": a["exits"],
                "longest_dwell_s": round(a["dwell_s_max"], 1),
                "restricted_intrusions": intr.get((cam_id, zone_id), 0)
                if zone_id
                else sum(n for (c, _z), n in intr.items() if c == cam_id),
                "coverage_pct": round(min(100.0, 100.0 * coverage_s / window_s), 1),
            }
        )
    return {
        "site_id": site_id,
        "start": start,
        "end": end,
        "provenance": "observed",
        "note": "Counts from first-party vision rollups. Coverage is the share of the window the "
        "camera produced frames; a low coverage means the camera could not see, not that the "
        "zone was empty.",
        "rows": rows,
    }


REPORT_COLUMNS = [
    "camera_id",
    "camera_name",
    "zone_id",
    "zone_name",
    "people_peak",
    "people_mean",
    "entries",
    "exits",
    "longest_dwell_s",
    "restricted_intrusions",
    "coverage_pct",
]


def report_to_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for row in report["rows"]:
        w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in REPORT_COLUMNS})
    return buf.getvalue()
