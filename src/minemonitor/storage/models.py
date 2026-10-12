"""SQLAlchemy 2.x ORM models.

Every operational table carries ``site_id`` (multi-tenant from day one) and every
query must be scoped by it. Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on PostgreSQL (production); portable JSON on other dialects (SQLite tests).
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Site(Base):
    """A mine site — the tenant root."""

    __tablename__ = "sites"

    site_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="Africa/Harare")


class Asset(Base):
    """A machine at a site (haul truck, excavator, light vehicle, ...)."""

    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), primary_key=True, index=True)
    asset_class: Mapped[str] = mapped_column(String, nullable=False, default="generic")
    make: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Operator(Base):
    """A person who operates a machine — the single home for personal data.

    Brief §4: operator identity is a foreign key, never a name in an event
    payload, so personal data lives in **one** table that can be exported or
    erased on request without rewriting history. ``operator_id`` is an opaque
    reference (never a name); the human-readable fields here are the only PII in
    the database. **No biometric or face data, ever** — that stays in the vendor
    gate appliance on the mine's own network.

    Erasure is a tombstone: the PII columns are nulled and ``erased_at`` is set,
    while the opaque id (and every historical foreign key that points at it) is
    preserved — so a data-subject deletion never corrupts derived analytics.
    """

    __tablename__ = "operators"

    # Opaque, globally-unique reference — never a name.
    operator_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False, index=True)
    # Personal data — nullable so erasure can tombstone them in place.
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    employee_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    contact: Mapped[str | None] = mapped_column(String, nullable=True)
    # Access-control status (not PII) — drives gate authorisation (FP-07). Defaults let an
    # existing install behave as "all inducted, none suspended" until set by an admin.
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inducted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Zone(Base):
    """A geofenced zone. Polygon stored as GeoJSON in JSONB (PostGIS deferred)."""

    __tablename__ = "zones"

    zone_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="generic")
    # GeoJSON polygon geometry (WGS84 lon/lat rings).
    geometry: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    # Rule payload — rules are data, not code (see brief §9).
    rules: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)


class ShiftDefinition(Base):
    """A configurable shift for a site — the primary operational unit.

    Shift *instances* are derived, never stored: given these definitions and a
    timestamp, the operations layer resolves which shift a moment falls in and
    its ``[start, end)`` window (crossing midnight where needed). Editing a
    definition only changes how future windows resolve; it never rewrites the
    immutable telemetry those windows summarise. Times are local to the site's
    timezone (``Site.timezone``).
    """

    __tablename__ = "shift_definitions"
    __table_args__ = (PrimaryKeyConstraint("site_id", "name", name="pk_shift_definitions"),)

    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), index=True)
    name: Mapped[str] = mapped_column(String)  # e.g. "day", "night"
    start_hour_local: Mapped[int] = mapped_column(Integer, nullable=False)  # 0–23
    duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Position(Base):
    """Raw GNSS telemetry. Backed by a TimescaleDB hypertable on ``ts``.

    Idempotency: the primary key ``(site_id, asset_id, ts)`` means replaying a
    position is a no-op via ``ON CONFLICT DO NOTHING`` at the ingest boundary.
    """

    __tablename__ = "positions"
    __table_args__ = (PrimaryKeyConstraint("site_id", "asset_id", "ts", name="pk_positions"),)

    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_kph: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    hdop: Mapped[float | None] = mapped_column(Float, nullable=True)
    satellites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ignition: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)


class Event(Base):
    """An ``event.v1`` row — the unified alarm queue."""

    __tablename__ = "events"
    # The alarm queue is read as "this site's events, newest first" on every
    # dashboard poll — a composite index keeps that ordered scan off the heap.
    __table_args__ = (Index("ix_events_site_ts", "site_id", "ts"),)

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Operator identity is a foreign key, never a name in the payload (brief §4).
    operator_id: Mapped[str | None] = mapped_column(
        ForeignKey("operators.operator_id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    acknowledged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssetZoneState(Base):
    """Crash-safe per-(asset, zone) state for the debounce/hysteresis engine.

    Kept in the database, not in memory, so an unclean restart resumes without
    re-flapping or losing an in-progress dwell/overspeed episode (brief §3).
    """

    __tablename__ = "asset_zone_state"
    __table_args__ = (
        PrimaryKeyConstraint("site_id", "asset_id", "zone_id", name="pk_asset_zone_state"),
    )

    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    zone_id: Mapped[str] = mapped_column(String, nullable=False)
    # Confirmed membership (post-debounce), and the debounce counters.
    inside: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consec_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consec_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Rule episode state, so each rule fires once per episode, not per fix.
    overspeed_consec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overspeed_fired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stationary_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dwell_fired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Last processed device time — later/out-of-order fixes are skipped for live
    # rule evaluation (they remain stored for retrospective recompute).
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HaulCycle(Base):
    """One computed haul cycle. Derived from positions; recomputable (brief §9)."""

    __tablename__ = "haul_cycles"
    __table_args__ = (
        PrimaryKeyConstraint("site_id", "asset_id", "start_ts", name="pk_haul_cycles"),
    )

    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    # The operator who drove this cycle, by opaque reference (brief §4).
    operator_id: Mapped[str | None] = mapped_column(
        ForeignKey("operators.operator_id"), nullable=True, index=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cycle_time_s: Mapped[float] = mapped_column(Float, nullable=False)
    queue_s: Mapped[float] = mapped_column(Float, nullable=False)
    load_s: Mapped[float] = mapped_column(Float, nullable=False)
    haul_s: Mapped[float] = mapped_column(Float, nullable=False)
    dump_s: Mapped[float] = mapped_column(Float, nullable=False)
    return_s: Mapped[float] = mapped_column(Float, nullable=False)


class RecomputeCursor(Base):
    """Per-asset progress of the incremental cycle/metric recompute (audit H3).

    The maintenance tick rebuilds each asset only from ``cycles_from`` (the entry of the
    cycle still in progress) and ``metrics_from`` (the bucket before the newest fix), not
    from the start of history. ``through_ts`` is the newest fix that run saw. Ingest lowers
    ``dirty_from`` to the device time of any fix stored *behind* ``through_ts`` (late or out
    of order), in the same transaction as the position, so the next run widens its window
    to cover it. Like positions, keyed by site without a foreign key: derived bookkeeping,
    no personal data.
    """

    __tablename__ = "recompute_cursors"
    __table_args__ = (PrimaryKeyConstraint("site_id", "asset_id", name="pk_recompute_cursors"),)

    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    cycles_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The newest fix was still inside the load visit at ``cycles_from``: while later fixes
    # stay in the load zone the run only rewrites buckets (a digger at the face all shift).
    in_open_visit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metrics_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    through_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dirty_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Load/dump zone geometry the cursor was computed against; an edit invalidates
    # ``entry_known`` (the entry may not be an entry under the new polygon).
    zones_sig: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssetMetrics(Base):
    """Per-asset rollup over a fixed bucket (asset.metrics.v1). Hypertable on bucket_start."""

    __tablename__ = "asset_metrics"
    __table_args__ = (
        PrimaryKeyConstraint("site_id", "asset_id", "bucket_start", name="pk_asset_metrics"),
    )

    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distance_m: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    moving_time_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    idle_time_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_speed_kph: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mean_speed_kph: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    zone_dwell_s: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    loads_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class User(Base):
    """An operator/supervisor/admin account for the API and dashboard.

    ``site_id`` scopes a user to one site; NULL means all sites (a global admin).
    The mine is the data controller — accounts are per site and auditable.
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # viewer|supervisor|admin|device
    site_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthLockout(Base):
    """Failed-login state per user, crash-safe in the database (brief §3).

    Brute-force protection: after N consecutive failures an account is locked
    until ``locked_until``. Kept in the DB, not in memory, so a restart does not
    reset an attacker's progress or wrongly free a locked account.
    """

    __tablename__ = "auth_lockout"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServiceHeartbeat(Base):
    """Liveness heartbeat for background workers (the ingestor).

    The worker upserts its timestamp each maintenance tick; ``/health`` reads it
    to tell a stuck or dead ingestor from a healthy one — the offline-detection
    and retention jobs are otherwise invisible to an HTTP health check.
    """

    __tablename__ = "service_heartbeat"

    service: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Incident(Base):
    """A managed investigation over an alarm — the operations lifecycle.

    An alarm (``Event``) says *something happened*; an incident tracks *what was
    done about it*: open → acknowledged → investigating → assigned → resolved →
    closed. The raw ``Event`` is **never mutated** — an incident links to it by
    ``event_id`` (nullable, so an incident can also be raised by hand). The
    per-incident timeline lives in ``incident_notes`` for full traceability.
    """

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_site_state", "site_id", "state"),)

    incident_id: Mapped[str] = mapped_column(String, primary_key=True)
    # Optimistic concurrency (brief §15): every UPDATE checks and bumps version_id,
    # so two operators who both act on a stale copy of the same incident cannot
    # silently overwrite one another — the second write raises StaleDataError, which
    # the router surfaces as 409 Conflict.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version_id}
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # The originating alarm, if any. Linked, never mutated (brief §6 / §4).
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.event_id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_category: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IncidentNote(Base):
    """Append-only timeline entry for an incident — a note or a state change.

    Never updated or deleted: the incident's full history (who did what, when,
    and every transition) is reconstructable from these rows.
    """

    __tablename__ = "incident_notes"
    __table_args__ = (Index("ix_incident_notes_incident", "incident_id", "ts"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), nullable=False)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "note" | "state_change"
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)


class DelayClassification(Base):
    """A human annotation explaining a period of lost time — stored **separately**
    from telemetry (brief: annotations never touch ``positions``).

    Why time was lost is a judgement a supervisor makes, not a fact a GNSS tracker
    measures. Keeping classifications in their own table means derived analytics
    stay reproducible from raw positions, and a reclassification never rewrites
    history. ``category`` is validated against a known list with ``other`` /
    ``unknown`` escape hatches; ``source`` marks manual vs. (future) auto-detected.
    """

    __tablename__ = "delay_classifications"
    __table_args__ = (Index("ix_delay_classifications_site_start", "site_id", "start_ts"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShiftHandover(Base):
    """An end-of-shift handover: what the outgoing crew saw, and the incoming
    crew's acknowledgement.

    An operational record (an annotation), stored separately from telemetry. The
    ``summary`` is a point-in-time snapshot of the shift scorecard captured when
    the handover was written — it is the record of *what was handed over*, so it
    stays fixed even as late data arrives; the live, recomputable scorecard is
    always available separately. Lifecycle is simply ``open`` → ``acknowledged``.
    """

    __tablename__ = "shift_handovers"
    __table_args__ = (Index("ix_shift_handovers_site_shift", "site_id", "shift_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    shift_id: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    summary: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    outgoing_by: Mapped[str] = mapped_column(String, nullable=False)
    outgoing_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    incoming_by: Mapped[str | None] = mapped_column(String, nullable=True)
    incoming_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BreachRecord(Base):
    """A personal-data breach the site, as data controller, is handling (CDPA, SI 155 of 2024).

    Records what happened, which classes of data and which people were affected (people as
    opaque operator ids in ``breach_operators``, so names stay in ``operators`` and erasure
    still works), and when the regulator and the people affected were told. The two notice
    deadlines are fixed when the breach is recorded, from the settings then in force, so a
    later settings change never moves the deadline of a breach already being handled.
    """

    __tablename__ = "breach_records"

    breach_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free text entered by an admin. Never copied into an alarm: alarms leave the box.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    data_classes: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    notify_individuals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    regulator_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Null when notice to the people affected is not required.
    individuals_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    regulator_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    regulator_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    individuals_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recorded_by: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    close_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BreachOperator(Base):
    """A person (operator id) affected by a breach."""

    __tablename__ = "breach_operators"

    breach_id: Mapped[str] = mapped_column(ForeignKey("breach_records.breach_id"), primary_key=True)
    operator_id: Mapped[str] = mapped_column(
        ForeignKey("operators.operator_id"), primary_key=True, index=True
    )


class AuditLog(Base):
    """Append-only audit trail: rule/zone changes, acks, retention runs, auth
    failures/lockouts, and access to personal data (operator reads, exports and
    erasures) — brief §4.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    site_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)


class Device(Base):
    """A provisioned telemetry device bound to exactly one asset (brief §10/§11).

    ``device_id`` is the identity a tracker authenticates to the broker with; it may
    publish telemetry only for its bound ``(site_id, asset_id)``. An asset has at most
    one device (the unique constraint), so provisioning is unambiguous and the
    broker ACL / ingest check can be derived directly from this table.
    """

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("site_id", "asset_id", name="uq_devices_site_asset"),
        Index("ix_devices_site", "site_id"),
    )

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_interval_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Mosquitto ``$7$`` PBKDF2-SHA512 hash of the device's broker password, rendered
    # into the broker password file (never the cleartext, which is shown once at
    # provisioning). Null until a secret is issued.
    broker_pw_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class Notification(Base):
    """A queued outbound alert for an event — the store-and-forward notification outbox.

    The network at the mine will fail (brief §3), so notifications are **not** sent
    inline when an event fires: one row is written per (event, channel, target) in the
    same transaction as the event, and a background dispatcher drains them with retry
    and backoff. A row therefore survives a crash or an outage and is delivered on
    recovery, exactly once per target (the unique constraint makes enqueue idempotent).

    The payload carries only ``event.v1`` fields — summary, type, severity, asset/zone,
    ids — never an operator name (personal data lives behind a foreign key, brief §4).
    Notifications are **advisory** like every output of this system: they warn a person,
    they never actuate plant (brief §15).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_id", "channel", "target", name="uq_notifications_event_channel"),
        Index("ix_notifications_state_next", "state", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)  # "webhook" | "email"
    target: Mapped[str] = mapped_column(String, nullable=False)  # URL or address
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FuelTank(Base):
    """A bulk fuel tank at a site (fuel domain, Phase 3). Reference data."""

    __tablename__ = "fuel_tanks"
    __table_args__ = (Index("ix_fuel_tanks_site", "site_id"),)

    tank_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    capacity_l: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FuelTransaction(Base):
    """A **measured** fuel movement — a machine observation, not an annotation.

    ``direction`` is ``dispense`` (fuel out of a tank into a machine) or ``delivery``
    (fuel into a tank). Litres and the optional odometer/engine-hour readings are
    *measured*; anything derived from them (efficiency) is calculated elsewhere and
    labelled as such. Records are append-only: a correction is a new, linked row, so
    historical reconciliation stays reproducible (brief §3/§15 discipline).
    """

    __tablename__ = "fuel_transactions"
    __table_args__ = (Index("ix_fuel_transactions_site_ts", "site_id", "ts"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)  # machine fuelled
    tank_id: Mapped[str | None] = mapped_column(String, nullable=True)  # source/target tank
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    litres: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False, default="dispense")
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    station: Mapped[str | None] = mapped_column(String, nullable=True)
    odometer_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    engine_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)


class FuelTankReading(Base):
    """A **measured** tank level reading (dip or telemetry), for reconciliation."""

    __tablename__ = "fuel_tank_readings"
    __table_args__ = (Index("ix_fuel_tank_readings_tank_ts", "tank_id", "ts"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    tank_id: Mapped[str] = mapped_column(ForeignKey("fuel_tanks.tank_id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level_l: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DispatchJob(Base):
    """A haulage demand: move ``material`` from a source to a dump (dispatch, Phase 6).

    Decision-support only. A job describes what needs hauling and how many concurrent
    trucks it wants; recommendations against it are advisory and require a supervisor's
    approval before they become dispatched instructions. The platform never controls a
    machine (brief §15).
    """

    __tablename__ = "dispatch_jobs"
    __table_args__ = (Index("ix_dispatch_jobs_site_status", "site_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    material: Mapped[str | None] = mapped_column(String, nullable=True)
    source_zone: Mapped[str | None] = mapped_column(String, nullable=True)  # loading point
    dest_zone: Mapped[str | None] = mapped_column(String, nullable=True)  # dump point
    target_trucks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DispatchAssignment(Base):
    """A recommended or approved truck→job pairing (dispatch, Phase 6).

    ``state`` flows recommended → approved → active → complete (or rejected). A
    ``recommended`` row is an **advisory** suggestion carrying its ``rationale`` (the
    evidence); it becomes a dispatched instruction only when a supervisor approves it.
    """

    __tablename__ = "dispatch_assignments"
    __table_args__ = (Index("ix_dispatch_assignments_site_state", "site_id", "state"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("dispatch_jobs.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="recommended")
    objective: Mapped[str] = mapped_column(String, nullable=False, default="balanced")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rationale: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    recommended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Weighbridge(Base):
    """A weighbridge/scale at a site (weighbridge domain, Phase 4). Reference data."""

    __tablename__ = "weighbridges"
    __table_args__ = (Index("ix_weighbridges_site", "site_id"),)

    scale_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WeighTicket(Base):
    """A **measured** weighbridge ticket — gross/tare/net for one weighing.

    Manufacturer-neutral: every scale/adapter maps onto this shape via the
    ``weighbridge.transaction.v1`` contract. Gross, tare and net are the measured
    figures the bridge prints; net-consistency (net ≈ gross − tare) is *flagged*, never
    silently corrected. ``ticket_no`` is unique per site, so re-importing a ticket (CSV
    replays) is idempotent rather than double-counting production.
    """

    __tablename__ = "weigh_tickets"
    __table_args__ = (
        UniqueConstraint("site_id", "ticket_no", name="uq_weigh_tickets_site_ticket"),
        Index("ix_weigh_tickets_site_ts", "site_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    ticket_no: Mapped[str] = mapped_column(String, nullable=False)
    scale_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False, default="outbound")
    gross_kg: Mapped[float] = mapped_column(Float, nullable=False)
    tare_kg: Mapped[float] = mapped_column(Float, nullable=False)
    net_kg: Mapped[float] = mapped_column(Float, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the vehicle
    trailer: Mapped[str | None] = mapped_column(String, nullable=True)
    material: Mapped[str | None] = mapped_column(String, nullable=True)
    destination: Mapped[str | None] = mapped_column(String, nullable=True)
    customer: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)


class MaintenancePlan(Base):
    """A service plan for an asset component (maintenance domain, Phase 5). Reference data.

    A plan is an interval — by engine hours, calendar days, or both — against which a
    **deterministic** health indicator is derived. No plan means health is ``Unknown``;
    the system never invents a schedule.
    """

    __tablename__ = "maintenance_plans"
    __table_args__ = (
        UniqueConstraint("site_id", "asset_id", "component", name="uq_maint_plan_asset_component"),
        Index("ix_maint_plans_site", "site_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    component: Mapped[str] = mapped_column(String, nullable=False, default="machine")
    interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkOrder(Base):
    """A maintenance event: a service, repair or inspection on an asset component.

    Completing a ``service`` work order (with the optional engine-hour reading taken at
    the time) is the baseline the health indicator measures from — so a fresh service
    resets the risk. An operational annotation, stored separately from telemetry.
    """

    __tablename__ = "work_orders"
    __table_args__ = (Index("ix_work_orders_site_asset", "site_id", "asset_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    component: Mapped[str] = mapped_column(String, nullable=False, default="machine")
    type: Mapped[str] = mapped_column(String, nullable=False, default="service")
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    at_engine_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Camera(Base):
    """A camera in the site estate, with AI-readiness metadata (Mine Monitor Vision, FP-01).

    Reference/config data — not telemetry, not an event, no video. It captures the RAN Mines
    Phase 0 camera audit (which of the estate can support AI analytics, on which stream, in
    which zone, with what calibration and health) and is the registry every vision capability
    reads from. ``stream_url`` is a secret and is never returned by the API. ``zone_id`` is a
    soft reference to a ``zones`` row (image-zone → operational zone mapping).
    """

    __tablename__ = "cameras"
    __table_args__ = (Index("ix_cameras_site", "site_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    location_description: Mapped[str | None] = mapped_column(String, nullable=True)
    make_model: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_url: Mapped[str | None] = mapped_column(String, nullable=True)  # secret; never returned
    stream_type: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    stream_kind: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec: Mapped[str | None] = mapped_column(String, nullable=True)
    lighting: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    has_usable_ai_stream: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_suitability: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    blind_spot_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    homography: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    calibration_status: Mapped[str] = mapped_column(String, nullable=False, default="none")
    model_deployed: Mapped[str | None] = mapped_column(String, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    health_state: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scene config (vision V1): image-zones in normalised image coords, each mapped to an
    # operational ``zones`` row — ``[{"zone_id", "zone_version", "polygon": [[x, y], ...]}]``.
    image_zones: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LaboratoryResult(Base):
    """A **measured** laboratory assay result, ingested directly from an instrument (FP-08).

    The immutable-original / reproducible-derived split the platform already enforces for
    telemetry applies here: ``value``/``element``/``unit`` are the parsed measured figures,
    while ``original`` holds the raw instrument record **write-once** and ``original_hash`` is
    its SHA-256 — so tampering with the stored original is detectable and the record can be
    reconciled against the source file. ``original_file_ref`` points at the full export in
    object storage (S3) once offloaded; null until then.

    Corrections **never** mutate this row — they are separate append-only
    ``LaboratoryCorrection`` entries. ``sample_ref`` is an opaque lab label, never personal
    data. ``result_id`` is deterministic so re-importing the same result is idempotent.
    """

    __tablename__ = "laboratory_results"
    __table_args__ = (
        Index("ix_laboratory_results_site", "site_id"),
        Index("ix_laboratory_results_site_ts", "site_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    instrument: Mapped[str] = mapped_column(String, nullable=False)
    sample_ref: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    element: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    method: Mapped[str | None] = mapped_column(String, nullable=True)
    batch_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # The immutable original: the raw instrument record (write-once) and its hash.
    original: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    original_hash: Mapped[str] = mapped_column(String, nullable=False)
    original_file_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="spectraa")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)


class LaboratoryCorrection(Base):
    """An **append-only** correction to a laboratory result — an annotation, never a mutation.

    A correction references the original ``LaboratoryResult`` by ``result_id`` and records the
    corrected value, the **actor** who made it and the reason. The original measured record is
    preserved untouched, so the full history — and "who changed it, when, and why" — is always
    reconstructable (the audit backbone FP-09 reconciliation depends on). Rows are never
    updated or deleted.
    """

    __tablename__ = "laboratory_corrections"
    __table_args__ = (
        Index("ix_laboratory_corrections_site", "site_id"),
        Index("ix_laboratory_corrections_result", "result_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    result_id: Mapped[str] = mapped_column(ForeignKey("laboratory_results.id"), nullable=False)
    corrected_value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccessEvent(Base):
    """A gate/turnstile access decision (access-control domain, FP-07).

    A record only — the gate hardware enforces; Mine Monitor records and reasons. **No
    biometric template or image, ever** (brief §4): ``credential_ref`` is an opaque
    tag/card/face-event id, and ``operator_ref`` is a soft reference to ``operators`` (kept a
    plain column, not a hard FK, so an event referencing an unknown/not-yet-provisioned person
    still ingests). ``decision`` is the source system's own decision; Mine Monitor's
    authorisation verdict is raised separately as an ``event.v1``.
    """

    __tablename__ = "access_events"
    # Selected searches neither completed nor yet escalated: all the missed-search check reads.
    __table_args__ = (
        Index(
            "ix_access_events_search_pending",
            "site_id",
            "ts",
            postgresql_where=text(
                "search_selected IS TRUE AND search_completed IS NOT TRUE "
                "AND search_escalated_at IS NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    gate_id: Mapped[str] = mapped_column(String, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    search_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    search_completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metal_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the tick raised this passage's skipped search (``search_missed``): raised once,
    # even after events retention purges the alarm.
    search_escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AccessQuarantine(Base):
    """A gate event refused at ingest because its device clock is too far ahead (plan G1).

    Not an access record: the passage's true time is unknown, so it is never stored in
    ``access_events`` (where it would move the poll cursor into the future and hide every later
    event). Keyed on the same deterministic id as the access event, so a vendor that re-presents
    the same event every poll is recognised and never re-alarms. Holds **no personal data**
    (no operator or credential reference); ``source_event_id`` finds the record on the gate
    appliance, which keeps it.
    """

    __tablename__ = "access_quarantine"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False, index=True)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String, nullable=False)
    gate_id: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    claimed_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    skew_s: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceStatus(Base):
    """Reachability of a polled external feed (plan G2), e.g. the Dahua gate API.

    Persisted so an outage keeps its start time across an ingestor restart or a power cut
    (brief §3). ``failing_since`` is the first failed poll of the current outage and is cleared
    by the next successful one; ``source_offline`` is raised once per outage from it.
    """

    __tablename__ = "source_status"

    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)


class GeneratorReading(Base):
    """A **measured** DSE genset controller reading (generator domain, FP-11).

    A DSE controller already measures engine hours, running state, fuel level and faults, so
    this is real measured data. Readings are write-once and idempotent on ``reading_id`` (a
    replay/backfill never double-counts or double-alarms). The latest ``engine_hours`` feeds the
    existing deterministic maintenance health (`maintenance.service.assess_health`); a ``fault``
    or an unexpected run raises an advisory ``event.v1``. Generators are ``assets`` with
    ``asset_class="generator"``. **Advisory only** — never controls a genset (brief §15).
    """

    __tablename__ = "generator_readings"
    __table_args__ = (
        Index("ix_generator_readings_site", "site_id"),
        Index("ix_generator_readings_asset_ts", "site_id", "asset_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="dse_sim")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engine_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    running: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fuel_level_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_running: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fault: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fault_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MaterialMeasurement(Base):
    """A **measured** quantity of material at a circuit stage (reconciliation domain, FP-09).

    The ingestion groundwork for gold reconciliation: measured quantities through
    ore→crusher→mill→tanks→carbon→furnace→output→gate, recorded with source provenance and an
    optional assay reference (→ FP-08 lab), so the later expected-vs-observed slices are
    reproducible from stored measurements. Write-once and idempotent on ``measurement_id`` (a
    re-import never double-counts production). This table holds only **measured** inputs — never
    an expectation, a variance, or a theft conclusion (that boundary lives in the deferred
    reconciliation engine; the platform flags, a human concludes).
    """

    __tablename__ = "material_measurements"
    __table_args__ = (
        Index("ix_material_measurements_site", "site_id"),
        Index("ix_material_measurements_site_stage_ts", "site_id", "stage", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    assay_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    quality: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)


class VisionVendorEvent(Base):
    """A normalised NVR AI event (vision domain, Stage A — ``docs/VISION_BUILD_GATE.md`` §4.5).

    The near-term, no-new-hardware vision path: the Dahua NVR already runs AI and emits smart
    events (line-crossing, intrusion, loitering) that currently go unused. We ingest each as a
    **vendor-inferred** record — a distinct provenance class from first-party perception (the
    vendor's model/thresholds are opaque; its ``vendor_confidence`` is carried verbatim and
    never upgraded). It is a record + the basis for an ``event.v1`` promotion; the gate
    hardware and the NVR remain the sensing authority.

    **No identity/biometric field, ever** (brief §4): identity-bearing NVR types are refused at
    the ingest boundary and never reach this table. ``vendor_event_id`` is deterministic so a
    replay/backfill of the same NVR event is idempotent. ``camera_id`` is a soft reference to a
    registered ``cameras`` row (a ``dahua_nvr:<channel>`` placeholder when unmapped).
    """

    __tablename__ = "vision_vendor_events"
    __table_args__ = (
        Index("ix_vision_vendor_events_site", "site_id"),
        Index("ix_vision_vendor_events_site_ts", "site_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String, nullable=False)
    camera_id: Mapped[str | None] = mapped_column(String, nullable=True)
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_type: Mapped[str] = mapped_column(String, nullable=False)
    normalized_type: Mapped[str] = mapped_column(String, nullable=False)
    vendor_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    vendor_rule_name: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clip_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------------------------
# First-party vision (Stage B — docs/VISION_BUILD_GATE.md §4). No table here can hold identity,
# a face, or frame bytes: objects are class + box + confidence + per-camera track id only.
# ---------------------------------------------------------------------------------------------


class VisionRuleVersion(Base):
    """An immutable temporal-rule configuration (``vision.rule_version.v1``).

    ``(site_id, rule_id, rule_version)`` is unique and never edited in place — a change is a new
    version. Only ``active_to`` may be set later (retiring a version). Every operational event
    cites one, so any alarm is reproducible from stored observations plus the rule that made it.
    """

    __tablename__ = "vision_rule_versions"
    __table_args__ = (
        UniqueConstraint("site_id", "rule_id", "rule_version", name="uq_vision_rule_version"),
        Index("ix_vision_rule_versions_site", "site_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    zone_version: Mapped[str | None] = mapped_column(String, nullable=True)
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VisionOperationalEvent(Base):
    """A rule's evidenced conclusion (``vision.operational_event.v1``). Derived, recomputable.

    ``promoted_event_id`` links to the ``event.v1`` alarm it raised, if the type warrants a human.
    """

    __tablename__ = "vision_operational_events"
    __table_args__ = (
        Index("ix_vision_operational_events_site_ts", "site_id", "ts"),
        Index("ix_vision_operational_events_site_camera", "site_id", "camera_id"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    provenance: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    zone_version: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    promoted_event_id: Mapped[str | None] = mapped_column(String, nullable=True)


class VisionObservation(Base):
    """One frame's perception (``vision.observation.v1``) — lab mode / pulled slices only.

    Backed by a TimescaleDB hypertable on ``ts`` (PK includes the partition column). ``objects``
    holds class/box/confidence/track only — the recompute basis for operational events.
    """

    __tablename__ = "vision_observations"
    __table_args__ = (
        PrimaryKeyConstraint("observation_id", "ts", name="pk_vision_observations"),
        Index("ix_vision_observations_site_camera_ts", "site_id", "camera_id", "ts"),
    )

    observation_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    site_id: Mapped[str] = mapped_column(String, nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frame_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    image: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    calibration_version: Mapped[str | None] = mapped_column(String, nullable=True)
    objects: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)


class VisionNodeHealth(Base):
    """An edge-node heartbeat (``vision.health.v1``)."""

    __tablename__ = "vision_node_health"
    __table_args__ = (Index("ix_vision_node_health_site_node_ts", "site_id", "node_id", "ts"),)

    health_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    cameras_active: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_mean: Mapped[float] = mapped_column(Float, nullable=False)
    queue_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    dropped_frames: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_ids: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class VisionCameraHealth(Base):
    """A per-camera quality assessment (``vision.camera_health.v1``)."""

    __tablename__ = "vision_camera_health"
    __table_args__ = (
        Index("ix_vision_camera_health_site_camera_ts", "site_id", "camera_id", "ts"),
    )

    health_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    blur_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    luma_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    frozen_frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VisionZoneRollup(Base):
    """Per camera × zone × bucket people counts (``vision.zone_rollup.v1``) — reporting source."""

    __tablename__ = "vision_zone_rollups"
    __table_args__ = (
        Index("ix_vision_zone_rollups_site_ts", "site_id", "ts"),
        Index("ix_vision_zone_rollups_site_zone_ts", "site_id", "zone_id", "ts"),
    )

    rollup_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_s: Mapped[int] = mapped_column(Integer, nullable=False)
    object_class: Mapped[str] = mapped_column(String, nullable=False)
    count_max: Mapped[int] = mapped_column(Integer, nullable=False)
    count_mean: Mapped[float] = mapped_column(Float, nullable=False)
    entries: Mapped[int] = mapped_column(Integer, nullable=False)
    exits: Mapped[int] = mapped_column(Integer, nullable=False)
    dwell_s_max: Mapped[float] = mapped_column(Float, nullable=False)
    dwell_s_p50: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_s: Mapped[float] = mapped_column(Float, nullable=False)


class VisionModelRecord(Base):
    """The vision model registry (``docs/VISION_ARCHITECTURE.md`` §6). Global, not per site.

    Every observation cites ``model.id``/``model.version``; this is where that id resolves to exact
    weights (``sha256``), licence, training data and measured performance. Rows are never edited in
    place for a new model — a new model is a new ``(model_id, version)``; promotion moves
    ``status``. A model whose licence is not on the permissive/accepted list can never reach
    ``production`` (enforced in ``vision.registry``).
    """

    __tablename__ = "vision_models"
    __table_args__ = (UniqueConstraint("model_id", "version", name="uq_vision_model_version"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    task: Mapped[str] = mapped_column(String, nullable=False)
    architecture: Mapped[str | None] = mapped_column(String, nullable=True)
    licence: Mapped[str] = mapped_column(String, nullable=False)
    weights_sha256: Mapped[str] = mapped_column(String, nullable=False)
    weights_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    training_dataset: Mapped[str | None] = mapped_column(String, nullable=True)
    classes: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    input_size: Mapped[list[int] | None] = mapped_column(JsonType, nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="experimental")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
