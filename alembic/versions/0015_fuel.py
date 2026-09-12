"""Fuel domain — measured transactions, tanks and tank readings (Phase 3).

Additive and safe for an existing install: three new tables only, no change to any
existing one. Fuel transactions are measured machine observations, stored separately
from telemetry and from operational annotations.

Revision ID: 0015_fuel
Revises: 0014_notifications
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_fuel"
down_revision: str | None = "0014_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fuel_tanks",
        sa.Column("tank_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("capacity_l", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("tank_id"),
    )
    op.create_index("ix_fuel_tanks_site", "fuel_tanks", ["site_id"])

    op.create_table(
        "fuel_transactions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("tank_id", sa.String(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("litres", sa.Float(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("station", sa.String(), nullable=True),
        sa.Column("odometer_km", sa.Float(), nullable=True),
        sa.Column("engine_hours", sa.Float(), nullable=True),
        sa.Column("unit_cost", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fuel_transactions_site_ts", "fuel_transactions", ["site_id", "ts"])

    op.create_table(
        "fuel_tank_readings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("tank_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level_l", sa.Float(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.ForeignKeyConstraint(["tank_id"], ["fuel_tanks.tank_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fuel_tank_readings_tank_ts", "fuel_tank_readings", ["tank_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_fuel_tank_readings_tank_ts", table_name="fuel_tank_readings")
    op.drop_table("fuel_tank_readings")
    op.drop_index("ix_fuel_transactions_site_ts", table_name="fuel_transactions")
    op.drop_table("fuel_transactions")
    op.drop_index("ix_fuel_tanks_site", table_name="fuel_tanks")
    op.drop_table("fuel_tanks")
