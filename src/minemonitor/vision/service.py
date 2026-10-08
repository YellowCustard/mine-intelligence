"""Core-side vision service: rule registry, idempotent ingest, alarm promotion. Callers commit.

The edge runs perception + tracking + the temporal rules and sends **thin contracts**
(``docs/VISION_BUILD_GATE.md`` §5): operational events, 5-min zone rollups, node/camera health,
and — in lab mode or when pulled for audit — per-frame observations. This module is where they
land in the core:

- **Validate or reject loudly.** Every payload is parsed by its registered ``vision.*.v1`` model
  (``extra="forbid"``); an unknown camera, an uncited rule version, or a rule/type mismatch is a
  :class:`VisionIngestError`, never a silent coercion.
- **Idempotent.** Every record carries a deterministic id; a replay/backfill after a link outage
  stores nothing new and raises no second alarm.
- **Promotion.** An operational event that warrants a human becomes a plain ``event.v1`` in the one
  alarm queue (``source = "vision:<camera_id>"``). Severity comes from **rule/zone policy**, never
  the model. The alarm carries the provenance label, confidence and a link back to its evidence.
  It never names a person and never claims a fleet asset — ``asset_id`` stays null.
- **The "missed" lane.** A camera that stops reporting health raises ``camera_offline``.

Advisory only: nothing here actuates plant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.contracts.event import EventType, Severity
from minemonitor.contracts.vision import (
    VisionCameraHealthV1,
    VisionHealthV1,
    VisionObservationV1,
    VisionOperationalEventV1,
    VisionRuleVersionV1,
    VisionZoneRollupV1,
)
from minemonitor.events.repository import alarmed_since, new_event_id, persist_event
from minemonitor.storage.models import (
    Camera,
    Event,
    VisionCameraHealth,
    VisionNodeHealth,
    VisionObservation,
    VisionOperationalEvent,
    VisionRuleVersion,
    VisionZoneRollup,
    Zone,
)
from minemonitor.zones.occupancy import cap_severity, parse_cap

SOURCE_PREFIX = "vision"

VisionRecord = (
    VisionOperationalEventV1
    | VisionZoneRollupV1
    | VisionHealthV1
    | VisionCameraHealthV1
    | VisionObservationV1
)

# Contracts the edge may send to the core. ``vision.vendor_event.v1`` has its own NVR path and
# ``vision.rule_version.v1`` is admin configuration, so neither is accepted from a device.
INGESTIBLE: dict[str, type[BaseModel]] = {
    "vision.operational_event.v1": VisionOperationalEventV1,
    "vision.zone_rollup.v1": VisionZoneRollupV1,
    "vision.health.v1": VisionHealthV1,
    "vision.camera_health.v1": VisionCameraHealthV1,
    "vision.observation.v1": VisionObservationV1,
}

# Operational types that warrant a human → the existing event.v1 type they promote to.
# Types absent here (zone_entry/zone_exit, queue states) feed analytics only.
PROMOTION: dict[str, EventType] = {
    "person_in_restricted_zone": "zone_breach",
    "vehicle_in_restricted_zone": "zone_breach",
    "person_dwell": "zone_dwell",
    "zone_occupancy_exceeded": "zone_occupancy",
    "camera_obstructed": "camera_obstructed",
    "unsafe_proximity_person_machine": "proximity",
}

_MACHINERY = [
    "haul_truck",
    "light_vehicle",
    "loader",
    "excavator",
    "dozer",
    "grader",
    "drill",
    "water_truck",
    "service_truck",
    "bus",
]

# The starting rule set (build-gate §7 profiles, person-first). Rules are data: an admin installs
# these once per site and changes them by publishing a new version, never by editing in place.
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "person_in_restricted_zone",
        "event_type": "person_in_restricted_zone",
        "parameters": {
            "classes": ["person"],
            "min_observations": 3,
            "min_dwell_ms": 1500,
            "hysteresis_observations": 3,
            "cooldown_ms": 60_000,
            "window_ms": 5000,
            "confidence_threshold": 0.5,
            "severity": "critical",
            "zone_kinds": ["restricted"],
        },
        "notes": "Build-gate §7 #1: fast, low miss-tolerance.",
    },
    {
        "rule_id": "vehicle_in_restricted_zone",
        "event_type": "vehicle_in_restricted_zone",
        "parameters": {
            "classes": _MACHINERY,
            "min_observations": 3,
            "min_dwell_ms": 2000,
            "hysteresis_observations": 3,
            "cooldown_ms": 60_000,
            "window_ms": 5000,
            "confidence_threshold": 0.5,
            "severity": "critical",
            "zone_kinds": ["restricted"],
        },
        "notes": "Build-gate §7 #2.",
    },
    {
        "rule_id": "person_dwell",
        "event_type": "person_dwell",
        "parameters": {
            "classes": ["person"],
            "min_observations": 5,
            "min_dwell_ms": 120_000,
            "hysteresis_observations": 3,
            "cooldown_ms": 300_000,
            "window_ms": 10_000,
            "confidence_threshold": 0.5,
            "severity": "warning",
            "zone_kinds": [],
        },
        "notes": "Loitering: a person held in any mapped zone beyond the dwell threshold.",
    },
    {
        "rule_id": "zone_occupancy_exceeded",
        "event_type": "zone_occupancy_exceeded",
        "parameters": {
            "classes": ["person"],
            "min_observations": 3,
            "min_dwell_ms": 3000,
            "hysteresis_observations": 3,
            "cooldown_ms": 120_000,
            "window_ms": 5000,
            "confidence_threshold": 0.5,
            "severity": "warning",
            "zone_kinds": [],
        },
        "notes": "Sustained count above the zone's max_occupancy; zone policy sets severity.",
    },
    {
        "rule_id": "camera_obstructed",
        "event_type": "camera_obstructed",
        "parameters": {
            "classes": ["person"],
            "min_observations": 10,
            "min_dwell_ms": 30_000,
            "hysteresis_observations": 10,
            "cooldown_ms": 600_000,
            "window_ms": 60_000,
            "confidence_threshold": 0.7,
            "severity": "warning",
            "zone_kinds": [],
        },
        "notes": "Build-gate §7 #10: coverage lost — the 'missed' lane.",
    },
]
DEFAULT_RULE_VERSION = "1.0.0"


class VisionIngestError(ValueError):
    """A vision payload the core refuses (malformed, unknown camera, uncited rule, …)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(ts: datetime) -> datetime:
    """SQLite drops tzinfo on read; treat naive stored datetimes as UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------------------------


def rule_row_id(site_id: str, rule_id: str, rule_version: str) -> str:
    return f"{site_id}:{rule_id}:{rule_version}"


def get_rule_version(
    session: Session, site_id: str, rule_id: str, rule_version: str
) -> VisionRuleVersion | None:
    return session.get(VisionRuleVersion, rule_row_id(site_id, rule_id, rule_version))


def list_rule_versions(
    session: Session, site_id: str, *, active_at: datetime | None = None
) -> list[VisionRuleVersion]:
    """All rule versions for a site, or only those active at ``active_at``."""
    rows = list(
        session.execute(
            select(VisionRuleVersion)
            .where(VisionRuleVersion.site_id == site_id)
            .order_by(VisionRuleVersion.rule_id, VisionRuleVersion.active_from)
        )
        .scalars()
        .all()
    )
    if active_at is None:
        return rows
    return [
        r
        for r in rows
        if _aware(r.active_from) <= active_at
        and (r.active_to is None or _aware(r.active_to) > active_at)
    ]


def create_rule_version(
    session: Session, rule: VisionRuleVersionV1, *, author: str
) -> VisionRuleVersion:
    """Publish a rule version. Immutable: an existing ``(site, rule_id, version)`` is refused."""
    if get_rule_version(session, rule.site_id, rule.rule_id, rule.rule_version) is not None:
        raise VisionIngestError(
            f"rule {rule.rule_id} {rule.rule_version} already exists — publish a new version"
        )
    row = VisionRuleVersion(
        id=rule_row_id(rule.site_id, rule.rule_id, rule.rule_version),
        site_id=rule.site_id,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        event_type=rule.event_type,
        parameters=rule.parameters.model_dump(),
        zone_version=rule.zone_version,
        active_from=rule.active_from,
        active_to=rule.active_to,
        author=rule.author or author,
        notes=rule.notes,
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def retire_rule_version(
    session: Session, site_id: str, rule_id: str, rule_version: str, *, at: datetime | None = None
) -> VisionRuleVersion:
    """Set ``active_to`` — the only mutation a rule version allows. Events citing it stay valid."""
    row = get_rule_version(session, site_id, rule_id, rule_version)
    if row is None:
        raise LookupError(f"rule {rule_id} {rule_version} not found")
    if row.active_to is None:
        row.active_to = at or _now()
    return row


def install_default_rules(
    session: Session, site_id: str, *, author: str, at: datetime | None = None
) -> list[VisionRuleVersion]:
    """Install the default rule set at ``1.0.0`` (idempotent: existing versions are skipped)."""
    created = []
    for spec in DEFAULT_RULES:
        if get_rule_version(session, site_id, spec["rule_id"], DEFAULT_RULE_VERSION) is not None:
            continue
        rule = VisionRuleVersionV1.model_validate(
            {
                **spec,
                "site_id": site_id,
                "rule_version": DEFAULT_RULE_VERSION,
                "active_from": at or _now(),
                "author": author,
            }
        )
        created.append(create_rule_version(session, rule, author=author))
    return created


def rule_to_contract(row: VisionRuleVersion) -> dict[str, Any]:
    """A stored rule version as its ``vision.rule_version.v1`` JSON shape."""
    return VisionRuleVersionV1.model_validate(
        {
            "site_id": row.site_id,
            "rule_id": row.rule_id,
            "rule_version": row.rule_version,
            "event_type": row.event_type,
            "parameters": row.parameters,
            "zone_version": row.zone_version,
            "active_from": _aware(row.active_from),
            "active_to": _aware(row.active_to) if row.active_to else None,
            "author": row.author,
            "notes": row.notes,
        }
    ).model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------------------------
# Edge configuration
# ---------------------------------------------------------------------------------------------


def edge_config(session: Session, site_id: str, *, at: datetime | None = None) -> dict[str, Any]:
    """What an edge node needs to run offline: scene zones, zone policy, active rules.

    Stream URLs are **not** included (they are secrets; the edge holds its own source config).
    """
    at = at or _now()
    cameras = (
        session.execute(select(Camera).where(Camera.site_id == site_id, Camera.enabled.is_(True)))
        .scalars()
        .all()
    )
    zone_ids = {z["zone_id"] for c in cameras for z in (c.image_zones or [])}
    zones = (
        session.execute(select(Zone).where(Zone.site_id == site_id, Zone.zone_id.in_(zone_ids)))
        .scalars()
        .all()
        if zone_ids
        else []
    )
    return {
        "site_id": site_id,
        "generated_at": at.isoformat(),
        "cameras": [
            {
                "camera_id": c.id,
                "name": c.name,
                "image_zones": c.image_zones or [],
                "calibration_status": c.calibration_status,
                "homography": c.homography if c.calibration_status == "calibrated" else None,
            }
            for c in cameras
            if c.image_zones
        ],
        "zones": {
            z.zone_id: {
                "name": z.name,
                "kind": z.kind,
                "max_occupancy": parse_cap(z.rules),
            }
            for z in zones
        },
        "rules": [rule_to_contract(r) for r in list_rule_versions(session, site_id, active_at=at)],
    }


# ---------------------------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------------------------


def parse(payload: dict[str, Any]) -> VisionRecord:
    """Parse one inbound payload by its ``schema``. Raises :class:`VisionIngestError`."""
    schema = payload.get("schema") if isinstance(payload, dict) else None
    model = INGESTIBLE.get(schema) if isinstance(schema, str) else None
    if model is None:
        raise VisionIngestError(f"not an ingestible vision contract: {schema!r}")
    try:
        return cast(VisionRecord, model.model_validate(payload))
    except ValidationError as exc:
        raise VisionIngestError(f"{schema}: {exc.errors(include_url=False)}") from exc


def _require_camera(session: Session, site_id: str, camera_id: str) -> Camera:
    cam = session.get(Camera, camera_id)
    if cam is None or cam.site_id != site_id:
        raise VisionIngestError(f"camera {camera_id!r} is not registered for site {site_id!r}")
    return cam


def ingest(
    session: Session, record: VisionRecord, *, received_at: datetime | None = None
) -> dict[str, Any]:
    """Store one parsed vision record idempotently; promote alarms. Does not commit.

    Returns ``{"schema", "id", "created", "alarm_event_id"}``.
    """
    received_at = received_at or _now()
    if isinstance(record, VisionOperationalEventV1):
        row, created, alarm = record_operational_event(session, record, received_at=received_at)
        return {
            "schema": "vision.operational_event.v1",
            "id": row.event_id,
            "created": created,
            "alarm_event_id": alarm,
        }
    if isinstance(record, VisionZoneRollupV1):
        _require_camera(session, record.site_id, record.camera_id)
        created = _insert_once(
            session,
            VisionZoneRollup,
            record.rollup_id,
            lambda: VisionZoneRollup(
                rollup_id=record.rollup_id,
                site_id=record.site_id,
                node_id=record.node_id,
                camera_id=record.camera_id,
                zone_id=record.zone_id,
                ts=record.ts,
                received_at=received_at,
                bucket_s=record.bucket_s,
                object_class=record.object_class,
                count_max=record.count_max,
                count_mean=record.count_mean,
                entries=record.entries,
                exits=record.exits,
                dwell_s_max=record.dwell_s_max,
                dwell_s_p50=record.dwell_s_p50,
                observed_s=record.observed_s,
            ),
        )
        return _result("vision.zone_rollup.v1", record.rollup_id, created)
    if isinstance(record, VisionHealthV1):
        created = _insert_once(
            session,
            VisionNodeHealth,
            record.health_id,
            lambda: VisionNodeHealth(
                health_id=record.health_id,
                site_id=record.site_id,
                node_id=record.node_id,
                ts=record.ts,
                received_at=received_at,
                status=record.status,
                cameras_active=record.cameras_active,
                fps_mean=record.fps_mean,
                queue_depth=record.queue_depth,
                dropped_frames=record.dropped_frames,
                cpu_pct=record.cpu_pct,
                mem_pct=record.mem_pct,
                gpu_pct=record.gpu_pct,
                model_ids=record.model_ids,
                last_error=record.last_error,
            ),
        )
        return _result("vision.health.v1", record.health_id, created)
    if isinstance(record, VisionCameraHealthV1):
        cam = _require_camera(session, record.site_id, record.camera_id)
        created = _insert_once(
            session,
            VisionCameraHealth,
            record.health_id,
            lambda: VisionCameraHealth(
                health_id=record.health_id,
                site_id=record.site_id,
                node_id=record.node_id,
                camera_id=record.camera_id,
                ts=record.ts,
                received_at=received_at,
                state=record.state,
                blur_score=record.blur_score,
                luma_mean=record.luma_mean,
                frozen_frames=record.frozen_frames,
                last_frame_at=record.last_frame_at,
            ),
        )
        if created:
            _update_camera_health(cam, record)
        return _result("vision.camera_health.v1", record.health_id, created)
    if isinstance(record, VisionObservationV1):
        _require_camera(session, record.site_id, record.camera_id)
        created = record_observation(session, record, received_at=received_at)
        return _result("vision.observation.v1", record.observation_id, created)
    raise VisionIngestError(f"unsupported record {type(record).__name__}")  # pragma: no cover


def _result(schema: str, id_: str, created: bool) -> dict[str, Any]:
    return {"schema": schema, "id": id_, "created": created, "alarm_event_id": None}


def _insert_once(session: Session, model: type, pk: str, build: Any) -> bool:
    """Insert unless a row with this id exists (idempotent ingest). Savepoint-safe on races."""
    if session.get(model, pk) is not None:
        return False
    try:
        with session.begin_nested():
            session.add(build())
    except Exception:
        if session.get(model, pk) is not None:
            return False  # a concurrent insert of the same id won the race — idempotent
        raise
    return True


def _update_camera_health(cam: Camera, record: VisionCameraHealthV1) -> None:
    """Mirror the latest assessment onto the camera registry (never moves it backwards)."""
    last = _aware(cam.last_seen) if cam.last_seen else None
    if last is None or record.ts >= last:
        cam.last_seen = record.ts
        cam.health_state = {
            "ok": "online",
            "no_signal": "offline",
            "unknown": "unknown",
        }.get(record.state, "degraded")


def record_observation(
    session: Session, record: VisionObservationV1, *, received_at: datetime
) -> bool:
    """Store one frame's perception (lab mode / pulled slice). Idempotent on ``observation_id``."""
    exists = session.execute(
        select(VisionObservation.observation_id).where(
            VisionObservation.observation_id == record.observation_id,
            VisionObservation.ts == record.ts,
        )
    ).first()
    if exists is not None:
        return False
    session.add(
        VisionObservation(
            observation_id=record.observation_id,
            ts=record.ts,
            site_id=record.site_id,
            node_id=record.node_id,
            camera_id=record.camera_id,
            received_at=received_at,
            frame_id=record.frame_id,
            model_id=record.model.id,
            model_version=record.model.version,
            image=record.image.model_dump() if record.image else None,
            calibration_version=record.calibration_version,
            objects=[o.model_dump(mode="json", by_alias=True) for o in record.objects],
        )
    )
    return True


# ---------------------------------------------------------------------------------------------
# Operational events → alarm promotion
# ---------------------------------------------------------------------------------------------


def record_operational_event(
    session: Session,
    record: VisionOperationalEventV1,
    *,
    received_at: datetime,
    promote: bool = True,
) -> tuple[VisionOperationalEvent, bool, str | None]:
    """Store an operational event and promote it to ``event.v1`` if its type warrants a human.

    Refuses an event that cites an unknown rule version or whose type differs from the rule's —
    no orphaned, unexplainable conclusions (build-gate §4.8). Idempotent on ``event_id``.
    """
    cam = _require_camera(session, record.site_id, record.camera_id)
    rule = get_rule_version(session, record.site_id, record.rule_id, record.rule_version)
    if rule is None:
        raise VisionIngestError(
            f"operational event cites unknown rule {record.rule_id} {record.rule_version}"
        )
    if rule.event_type != record.type:
        raise VisionIngestError(
            f"rule {record.rule_id} produces {rule.event_type!r}, not {record.type!r}"
        )
    existing = session.get(VisionOperationalEvent, record.event_id)
    if existing is not None:
        return existing, False, existing.promoted_event_id
    row = VisionOperationalEvent(
        event_id=record.event_id,
        site_id=record.site_id,
        node_id=record.node_id,
        camera_id=record.camera_id,
        ts=record.ts,
        received_at=received_at,
        type=record.type,
        provenance=record.provenance,
        confidence=record.confidence,
        rule_id=record.rule_id,
        rule_version=record.rule_version,
        zone_id=record.zone_id,
        zone_version=record.zone_version,
        subject=record.subject.model_dump() if record.subject else None,
        evidence=record.evidence.model_dump(mode="json"),
    )
    session.add(row)
    if promote:
        row.promoted_event_id = _promote(session, record, rule, cam)
    session.flush()
    return row, True, row.promoted_event_id


def alarm_id_for(operational_event_id: str) -> str:
    """Deterministic alarm id — a replayed operational event never double-alarms."""
    return f"vis-{operational_event_id}"


def _promote(
    session: Session, record: VisionOperationalEventV1, rule: VisionRuleVersion, cam: Camera
) -> str | None:
    alarm_type = PROMOTION.get(record.type)
    if alarm_type is None:
        return None  # analytics-only type
    alarm_id = alarm_id_for(record.event_id)
    if session.get(Event, alarm_id) is not None:
        return alarm_id
    zone = (
        session.get(Zone, {"zone_id": record.zone_id, "site_id": record.site_id})
        if record.zone_id
        else None
    )
    severity = str(rule.parameters.get("severity", "warning"))
    if record.type == "zone_occupancy_exceeded" and zone is not None and parse_cap(zone.rules):
        severity = cap_severity(zone.rules)  # zone policy owns occupancy severity
    signals = record.evidence.signals
    detail: dict[str, Any] = {
        "provenance": record.provenance,
        "confidence": record.confidence,
        "rule_id": record.rule_id,
        "rule_version": record.rule_version,
        "camera_id": record.camera_id,
        "operational_type": record.type,
    }
    if record.subject and record.subject.track_id:
        detail["track_id"] = record.subject.track_id
    for key in ("dwell_ms", "count", "max_occupancy", "observations", "min_conf"):
        if key in signals:
            detail[key] = signals[key]
    event = EventV1.model_validate(
        {
            "schema": "event.v1",
            "event_id": alarm_id,
            "site_id": record.site_id,
            "ts": record.ts,
            "type": alarm_type,
            "severity": cast(Severity, severity),
            "asset_id": None,  # vision never claims a fleet asset without fusion
            "zone_id": record.zone_id,
            "source": f"{SOURCE_PREFIX}:{record.camera_id}",
            "summary": _summary(record, cam, zone),
            "detail": detail,
            "evidence": {
                "vision_operational_event_id": record.event_id,
                "clip_uri": record.evidence.clip_ref,
            },
            "advisory": True,
            "state": "open",
        }
    )
    persist_event(session, event)
    return alarm_id


def _summary(record: VisionOperationalEventV1, cam: Camera, zone: Zone | None) -> str:
    where = zone.name if zone is not None else (record.zone_id or "view")
    signals = record.evidence.signals
    if record.type == "person_in_restricted_zone":
        return f"Person in restricted zone {where} ({cam.name})"
    if record.type == "vehicle_in_restricted_zone":
        return f"Vehicle in restricted zone {where} ({cam.name})"
    if record.type == "person_dwell":
        secs = int(signals.get("dwell_ms", 0) // 1000)
        return f"Person loitering {secs}s in {where} ({cam.name})"
    if record.type == "zone_occupancy_exceeded":
        return (
            f"{signals.get('count', '?')} people in {where} exceeds capacity "
            f"{signals.get('max_occupancy', '?')} ({cam.name})"
        )
    if record.type == "camera_obstructed":
        return f"Camera {cam.name} view obstructed — coverage lost"
    return f"{record.type.replace('_', ' ')} at {where} ({cam.name})"


# ---------------------------------------------------------------------------------------------
# Recompute (build-gate §5: stored observations + a versioned rule reproduce every conclusion)
# ---------------------------------------------------------------------------------------------


def recompute(
    session: Session,
    site_id: str,
    *,
    camera_id: str,
    since: datetime,
    until: datetime,
    rule_id: str,
    rule_version: str,
) -> dict[str, Any]:
    """Replay stored observations for one camera through one rule version. Does not commit.

    Produces operational events under that rule version beside any earlier ones (ids embed the
    version, so nothing is overwritten) and **does not raise alarms** — recomputing history is
    analysis, not a live condition. Only observations actually stored in the core (lab mode or a
    pulled slice) can be replayed.
    """
    from minemonitor.vision.temporal import replay

    cam = _require_camera(session, site_id, camera_id)
    rule = get_rule_version(session, site_id, rule_id, rule_version)
    if rule is None:
        raise LookupError(f"rule {rule_id} {rule_version} not found")
    rows = (
        session.execute(
            select(VisionObservation)
            .where(
                VisionObservation.site_id == site_id,
                VisionObservation.camera_id == camera_id,
                VisionObservation.ts >= since,
                VisionObservation.ts <= until,
            )
            .order_by(VisionObservation.ts)
        )
        .scalars()
        .all()
    )
    observations = [
        VisionObservationV1.model_validate(
            {
                "schema": "vision.observation.v1",
                "observation_id": r.observation_id,
                "site_id": r.site_id,
                "node_id": r.node_id,
                "camera_id": r.camera_id,
                "ts": _aware(r.ts),
                "frame_id": r.frame_id,
                "model": {"id": r.model_id, "version": r.model_version},
                "image": r.image,
                "calibration_version": r.calibration_version,
                "objects": r.objects,
                "advisory": True,
            }
        )
        for r in rows
    ]
    zone_ids = [z["zone_id"] for z in (cam.image_zones or [])]
    zones = {
        z.zone_id: {"name": z.name, "kind": z.kind, "max_occupancy": parse_cap(z.rules)}
        for z in session.execute(
            select(Zone).where(Zone.site_id == site_id, Zone.zone_id.in_(zone_ids))
        ).scalars()
    }
    events = replay(
        observations,
        rules=[VisionRuleVersionV1.model_validate(rule_to_contract(rule))],
        zones=zones,
        zone_versions={z["zone_id"]: z.get("zone_version", "") for z in (cam.image_zones or [])},
    )
    received_at = _now()
    created = []
    for ev in events:
        _row, was_new, _ = record_operational_event(
            session, ev, received_at=received_at, promote=False
        )
        if was_new:
            created.append(ev.event_id)
    return {
        "observations": len(observations),
        "events": [e.event_id for e in events],
        "created": created,
    }


# ---------------------------------------------------------------------------------------------
# The "missed" lane: a camera that stops reporting
# ---------------------------------------------------------------------------------------------


def _streamless() -> Any:
    """A health row saying *no frames are arriving*: ``no_signal`` with no ``last_frame_at``.

    The edge also reports ``no_signal`` for a **frozen picture** (identical frames), but then it
    carries the frame's timestamp. A frozen picture can be a quiet static scene under heavy
    compression, so it stays a visible camera condition and never pages anyone as offline.
    """
    return (VisionCameraHealth.state == "no_signal") & VisionCameraHealth.last_frame_at.is_(None)


def _no_signal_since(session: Session, scope: tuple[Any, ...]) -> datetime | None:
    """Start of the current unbroken run of stream-less health in a camera's history."""
    last_good = (
        session.execute(select(func.max(VisionCameraHealth.ts)).where(*scope, ~_streamless()))
        .scalars()
        .first()
    )
    q = select(func.min(VisionCameraHealth.ts)).where(*scope, _streamless())
    if last_good is not None:
        q = q.where(VisionCameraHealth.ts > last_good)
    first = session.execute(q).scalars().first()
    return _aware(first) if first is not None else None


def detect_camera_offline(
    session: Session, site_id: str, *, stale_s: int, now: datetime | None = None
) -> list[EventV1]:
    """Raise ``camera_offline`` for commissioned cameras whose health went silent. Commits.

    Only cameras that have reported at least once are considered (a camera never commissioned is
    not "offline"). One alarm per blind window; a camera that recovers and goes dark again raises
    a new one.
    """
    now = now or _now()
    cutoff = now - timedelta(seconds=stale_s)
    cams = (
        session.execute(select(Camera).where(Camera.site_id == site_id, Camera.enabled.is_(True)))
        .scalars()
        .all()
    )
    events: list[EventV1] = []
    for cam in cams:
        scope = (
            VisionCameraHealth.site_id == site_id,
            VisionCameraHealth.camera_id == cam.id,
        )
        latest = session.execute(
            select(
                VisionCameraHealth.ts,
                VisionCameraHealth.state,
                VisionCameraHealth.last_frame_at,
            )
            .where(*scope)
            .order_by(VisionCameraHealth.ts.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        last = _aware(latest.ts)
        reason = "silent"
        streamless = latest.state == "no_signal" and latest.last_frame_at is None
        down_since = _no_signal_since(session, scope) if streamless else None
        if last >= cutoff:
            # Health is fresh — but a node reporting that no frames arrive is just as blind as a
            # silent one. Offline once the stream has been down for the whole staleness window.
            if down_since is None or down_since >= cutoff:
                continue
            last, reason = down_since, "no_signal"
        # One alarm per blind window: it began when the stream died or, if frames were still
        # arriving, at the last health report. A no-signal camera whose node then goes silent is
        # the same window; a camera that recovered and went dark again is a new one.
        blind_since = down_since if down_since is not None else last
        if alarmed_since(
            session, site_id, "camera_offline", blind_since, source=f"{SOURCE_PREFIX}:{cam.id}"
        ):
            continue
        silent_s = int((now - last).total_seconds())
        ev = EventV1(
            schema="event.v1",
            event_id=new_event_id(),
            site_id=site_id,
            ts=now,
            type="camera_offline",
            severity="warning",
            asset_id=None,
            zone_id=None,
            source=f"{SOURCE_PREFIX}:{cam.id}",
            summary=(
                f"Camera {cam.name} silent for {silent_s}s — no vision coverage"
                if reason == "silent"
                else f"Camera {cam.name} has had no signal for {silent_s}s — no vision coverage"
            ),
            detail={
                "camera_id": cam.id,
                "reason": reason,
                "last_health_at": last.isoformat(),
                "silent_s": silent_s,
                "provenance": "inferred",
            },
            advisory=True,
            state="open",
        )
        persist_event(session, ev)
        cam.health_state = "offline"
        events.append(ev)
    session.commit()
    return events


# ---------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------


def list_operational_events(
    session: Session,
    site_id: str,
    *,
    camera_id: str | None = None,
    type_: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[VisionOperationalEvent]:
    stmt = select(VisionOperationalEvent).where(VisionOperationalEvent.site_id == site_id)
    if camera_id:
        stmt = stmt.where(VisionOperationalEvent.camera_id == camera_id)
    if type_:
        stmt = stmt.where(VisionOperationalEvent.type == type_)
    if since:
        stmt = stmt.where(VisionOperationalEvent.ts >= since)
    stmt = stmt.order_by(VisionOperationalEvent.ts.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def list_rollups(
    session: Session,
    site_id: str,
    *,
    camera_id: str | None = None,
    zone_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 2000,
) -> list[VisionZoneRollup]:
    stmt = select(VisionZoneRollup).where(VisionZoneRollup.site_id == site_id)
    if camera_id:
        stmt = stmt.where(VisionZoneRollup.camera_id == camera_id)
    if zone_id:
        stmt = stmt.where(VisionZoneRollup.zone_id == zone_id)
    if since:
        stmt = stmt.where(VisionZoneRollup.ts >= since)
    if until:
        stmt = stmt.where(VisionZoneRollup.ts < until)
    stmt = stmt.order_by(VisionZoneRollup.ts).limit(limit)
    return list(session.execute(stmt).scalars().all())


def latest_health(session: Session, site_id: str) -> dict[str, Any]:
    """The latest heartbeat per node and assessment per camera."""
    nodes: dict[str, VisionNodeHealth] = {}
    for n in session.execute(
        select(VisionNodeHealth)
        .where(VisionNodeHealth.site_id == site_id)
        .order_by(VisionNodeHealth.ts)
    ).scalars():
        nodes[n.node_id] = n
    cams: dict[str, VisionCameraHealth] = {}
    for c in session.execute(
        select(VisionCameraHealth)
        .where(VisionCameraHealth.site_id == site_id)
        .order_by(VisionCameraHealth.ts)
    ).scalars():
        cams[c.camera_id] = c
    return {
        "nodes": [
            {
                "node_id": n.node_id,
                "ts": _aware(n.ts),
                "status": n.status,
                "cameras_active": n.cameras_active,
                "fps_mean": n.fps_mean,
                "queue_depth": n.queue_depth,
                "dropped_frames": n.dropped_frames,
                "cpu_pct": n.cpu_pct,
                "mem_pct": n.mem_pct,
                "model_ids": n.model_ids or [],
                "last_error": n.last_error,
            }
            for n in nodes.values()
        ],
        "cameras": [
            {
                "camera_id": c.camera_id,
                "node_id": c.node_id,
                "ts": _aware(c.ts),
                "state": c.state,
                "blur_score": c.blur_score,
                "luma_mean": c.luma_mean,
                "frozen_frames": c.frozen_frames,
            }
            for c in cams.values()
        ],
    }
