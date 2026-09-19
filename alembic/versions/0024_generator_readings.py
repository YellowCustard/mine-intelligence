"""Generator monitoring, slice 1 (FP-11): measured DSE controller readings.

Additive and safe for an existing install: one new table. ``generator_readings`` stores the
measured DSE genset controller reading (engine hours, running state, fuel level, fault); it
feeds the existing maintenance run-hours health and raises advisory fault/unexpected-run alarms.

Revision ID: 0024_generator_readings
Revises: 0023_vision_vendor_events
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_generator_readings"
down_revision: str | None = "0023_vision_vendor_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generator_readings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="dse_sim"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engine_hours", sa.Float(), nullable=True),
        sa.Column("running", sa.Boolean(), nullable=False),
        sa.Column("fuel_level_pct", sa.Float(), nullable=True),
        sa.Column("expected_running", sa.Boolean(), nullable=True),
        sa.Column("fault", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fault_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generator_readings_site", "generator_readings", ["site_id"])
    op.create_index(
        "ix_generator_readings_asset_ts", "generator_readings", ["site_id", "asset_id", "ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_generator_readings_asset_ts", table_name="generator_readings")
    op.drop_index("ix_generator_readings_site", table_name="generator_readings")
    op.drop_table("generator_readings")
