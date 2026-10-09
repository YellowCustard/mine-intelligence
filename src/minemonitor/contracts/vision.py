"""Vision contracts. Currently: ``vision.vendor_event.v1`` (NVR event ingestion, Stage A).

The near-term vision path (``docs/VISION_BUILD_GATE.md`` §2/§4.5/§8) ingests the AI events the
Dahua NVR already produces. It is **vendor event ingestion, not first-party perception**: the
provenance is a distinct ``vendor_inferred`` class, the vendor's confidence is carried verbatim
and never upgraded to a first-party confidence, and **no identity/biometric field is ever
admitted** (``CLAUDE.md`` §4).

First-party perception contracts (Stage B, authorised at the 2026-09-28 gate —
``docs/VISION_BUILD_GATE.md`` §3/§4) follow the platform envelope spelling (``schema``,
``site_id``, ``ts`` = capture time, ``received_at`` = core arrival, ``advisory``):

- ``vision.observation.v1`` — what a model saw in one frame. ``provenance`` is always
  ``observed``; ``track_id`` is per-camera temporal identity, **never** an asset or a person's
  identity. Lab/on-demand transport only (§5): the edge keeps these locally.
- ``vision.operational_event.v1`` — a conclusion from a **named, versioned rule** with cited
  evidence; never ``observed``, never from one frame.
- ``vision.health.v1`` / ``vision.camera_health.v1`` — edge-node and per-camera health.
- ``vision.rule_version.v1`` — the immutable rule configuration an operational event cites.
- ``vision.zone_rollup.v1`` — per camera × zone × bucket people counts; what reporting reads.

No contract here has a field that could carry identity, a face, or frame bytes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class VisionVendorEventV1(BaseModel):
    """A normalised NVR AI event (vision domain, Stage A). Vendor-inferred provenance."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["vision.vendor_event.v1"] = Field(
        default="vision.vendor_event.v1", alias="schema"
    )
    vendor_event_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    camera_id: str | None = None
    channel: str | None = None
    vendor_type: str = Field(min_length=1)
    normalized_type: str = Field(min_length=1)
    # The vendor's own confidence — carried, NEVER upgraded to a first-party confidence.
    vendor_confidence: float | None = Field(default=None, ge=0, le=1)
    vendor_rule_name: str | None = None
    ts: AwareDatetime
    clip_ref: str | None = None
    provenance: Literal["vendor_inferred"] = "vendor_inferred"
    advisory: Literal[True] = True


# ---------------------------------------------------------------------------------------------
# First-party perception (Stage B). Ontology: docs/VISION_BUILD_GATE.md §6.
# ---------------------------------------------------------------------------------------------

ObjectClass = Literal[
    "person",
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
OperationalType = Literal[
    "zone_entry",
    "zone_exit",
    "person_in_restricted_zone",
    "vehicle_in_restricted_zone",
    "person_dwell",
    "zone_occupancy_exceeded",
    "camera_obstructed",
    "unsafe_proximity_person_machine",
    "machine_stationary_in_queue_zone",
]
DerivedProvenance = Literal["inferred", "correlated", "estimated"]
RuleSeverity = Literal["info", "warning", "critical"]
CameraState = Literal["ok", "obstructed", "defocused", "dark", "no_signal", "unknown"]
NodeStatus = Literal["ok", "degraded", "down"]

_SEMVER = r"^\d+\.\d+\.\d+$"
_SLUG = r"^[a-z][a-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BBox(_Strict):
    """Normalised image box, ``[0,1]`` on both axes, ``x1<x2`` and ``y1<y2``."""

    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> BBox:
        if not (self.x1 < self.x2 and self.y1 < self.y2):
            raise ValueError("bbox must satisfy x1<x2 and y1<y2")
        return self


class GroundPoint(_Strict):
    """A projected ground position. ``basis=unknown`` means no calibration — no distance claims."""

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    basis: Literal["homography", "unknown"]


class ModelRef(_Strict):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ImageSize(_Strict):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ObservedObject(_Strict):
    """One detected object in one frame. Perception only: class, box, confidence, track."""

    track_id: str = Field(min_length=3)
    class_: ObjectClass = Field(alias="class")
    confidence: float = Field(ge=0, le=1)
    bbox: BBox
    ground_point: GroundPoint | None = None
    zone_ids: list[str] = Field(default_factory=list)
    provenance: Literal["observed"] = "observed"


class VisionObservationV1(_Strict):
    """What a model saw in one frame (``docs/VISION_BUILD_GATE.md`` §4.1)."""

    schema_: Literal["vision.observation.v1"] = Field(
        default="vision.observation.v1", alias="schema"
    )
    observation_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    ts: AwareDatetime
    received_at: AwareDatetime | None = None
    frame_id: str = Field(min_length=1)
    model: ModelRef
    image: ImageSize | None = None
    calibration_version: str | None = None
    objects: list[ObservedObject]
    advisory: Literal[True] = True

    @model_validator(mode="after")
    def _tracks_namespaced(self) -> VisionObservationV1:
        prefix = f"{self.camera_id}:"
        for obj in self.objects:
            if not obj.track_id.startswith(prefix) or len(obj.track_id) == len(prefix):
                raise ValueError(f"track_id {obj.track_id!r} must be namespaced '{prefix}<n>'")
        return self


class Subject(_Strict):
    """The subject of an operational event. ``asset_id`` only with an association label."""

    track_id: str | None = None
    asset_id: str | None = None
    association: Literal["confirmed", "probable", "possible", "unknown"] | None = None

    @model_validator(mode="after")
    def _asset_needs_association(self) -> Subject:
        if self.asset_id is not None and self.association is None:
            raise ValueError("subject.asset_id requires an association label")
        return self


class Evidence(_Strict):
    """Why an operational event was concluded. Mandatory; never empty of a window."""

    observation_window: tuple[datetime, datetime]
    track_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    # A pointer, never bytes: the scheme is allow-listed (edge ring buffer, object store, NVR).
    clip_ref: str | None = Field(default=None, max_length=512, pattern=r"^(edge|s3|nvr)://\S+$")

    @model_validator(mode="after")
    def _window_ordered(self) -> Evidence:
        start, end = self.observation_window
        if end < start:
            raise ValueError("evidence.observation_window end precedes start")
        return self


class VisionOperationalEventV1(_Strict):
    """A rule's conclusion over observations, with evidence (§4.2). Never ``observed``."""

    schema_: Literal["vision.operational_event.v1"] = Field(
        default="vision.operational_event.v1", alias="schema"
    )
    event_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    ts: AwareDatetime
    received_at: AwareDatetime | None = None
    type: OperationalType
    provenance: DerivedProvenance
    confidence: float = Field(ge=0, le=1)
    rule_id: str = Field(pattern=_SLUG)
    rule_version: str = Field(pattern=_SEMVER)
    subject: Subject | None = None
    zone_id: str | None = None
    zone_version: str | None = None
    evidence: Evidence
    advisory: Literal[True] = True


class VisionHealthV1(_Strict):
    """Edge-node liveness/throughput (§4.3). Absence beyond a threshold is itself a signal."""

    schema_: Literal["vision.health.v1"] = Field(default="vision.health.v1", alias="schema")
    health_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    ts: AwareDatetime
    received_at: AwareDatetime | None = None
    status: NodeStatus
    cameras_active: int = Field(ge=0)
    fps_mean: float = Field(ge=0)
    queue_depth: int = Field(ge=0)
    dropped_frames: int = Field(ge=0)
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    mem_pct: float | None = Field(default=None, ge=0, le=100)
    gpu_pct: float | None = Field(default=None, ge=0, le=100)
    model_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None
    advisory: Literal[True] = True


class VisionCameraHealthV1(_Strict):
    """Per-camera usability from cheap frame-quality checks (§4.4), distinct from node health."""

    schema_: Literal["vision.camera_health.v1"] = Field(
        default="vision.camera_health.v1", alias="schema"
    )
    health_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    ts: AwareDatetime
    received_at: AwareDatetime | None = None
    state: CameraState
    blur_score: float | None = Field(default=None, ge=0)
    luma_mean: float | None = Field(default=None, ge=0, le=255)
    frozen_frames: int | None = Field(default=None, ge=0)
    last_frame_at: AwareDatetime | None = None
    advisory: Literal[True] = True


class RuleParameters(_Strict):
    """A rule's temporal profile (§7) — per event type, never one global dwell."""

    classes: list[ObjectClass] = Field(min_length=1)
    min_observations: int = Field(ge=1)
    min_dwell_ms: int = Field(ge=0)
    hysteresis_observations: int = Field(ge=0)
    cooldown_ms: int = Field(ge=0)
    window_ms: int = Field(ge=0)
    confidence_threshold: float = Field(ge=0, le=1)
    severity: RuleSeverity
    zone_kinds: list[str] = Field(default_factory=list)
    requires: list[Literal["ground_calibration"]] = Field(default_factory=list)


class VisionRuleVersionV1(_Strict):
    """The immutable rule configuration an operational event cites (§4.8)."""

    schema_: Literal["vision.rule_version.v1"] = Field(
        default="vision.rule_version.v1", alias="schema"
    )
    site_id: str = Field(min_length=1)
    rule_id: str = Field(pattern=_SLUG)
    rule_version: str = Field(pattern=_SEMVER)
    event_type: OperationalType
    parameters: RuleParameters
    zone_version: str | None = None
    active_from: AwareDatetime
    active_to: AwareDatetime | None = None
    author: str | None = None
    notes: str | None = None
    advisory: Literal[True] = True


class VisionZoneRollupV1(_Strict):
    """Per camera × zone × bucket counts (§5 rollups) — what vision reporting reads.

    ``zone_id`` null = the whole camera view. ``observed_s`` is how many seconds of the bucket the
    camera actually produced frames — coverage, so a quiet zone is distinguishable from a blind one.
    """

    schema_: Literal["vision.zone_rollup.v1"] = Field(
        default="vision.zone_rollup.v1", alias="schema"
    )
    rollup_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    zone_id: str | None = None
    ts: AwareDatetime
    received_at: AwareDatetime | None = None
    bucket_s: int = Field(gt=0)
    object_class: ObjectClass = "person"
    count_max: int = Field(ge=0)
    count_mean: float = Field(ge=0)
    entries: int = Field(ge=0)
    exits: int = Field(ge=0)
    dwell_s_max: float = Field(ge=0)
    dwell_s_p50: float | None = Field(default=None, ge=0)
    observed_s: float = Field(ge=0)
    advisory: Literal[True] = True

    @model_validator(mode="after")
    def _coverage_fits_bucket(self) -> VisionZoneRollupV1:
        if self.observed_s > self.bucket_s:
            raise ValueError("observed_s cannot exceed bucket_s")
        if self.count_mean > self.count_max:
            raise ValueError("count_mean cannot exceed count_max")
        return self
