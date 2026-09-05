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

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    acknowledged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
