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
