"""Haul-cycle and per-bucket metrics tables.

Revision ID: 0003_cycles_metrics
Revises: 0002_zones_state
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_cycles_metrics"
down_revision: str | None = "0002_zones_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "haul_cycles",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_time_s", sa.Float(), nullable=False),
        sa.Column("queue_s", sa.Float(), nullable=False),
        sa.Column("load_s", sa.Float(), nullable=False),
        sa.Column("haul_s", sa.Float(), nullable=False),
        sa.Column("dump_s", sa.Float(), nullable=False),
        sa.Column("return_s", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("site_id", "asset_id", "start_ts", name="pk_haul_cycles"),
    )
    op.create_index("ix_haul_cycles_site_start", "haul_cycles", ["site_id", "start_ts"])

    op.create_table(
        "asset_metrics",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=False, server_default="0"),
        sa.Column("moving_time_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("idle_time_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_speed_kph", sa.Float(), nullable=False, server_default="0"),
        sa.Column("mean_speed_kph", sa.Float(), nullable=False, server_default="0"),
        sa.Column("zone_dwell_s", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("loads_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("site_id", "asset_id", "bucket_start", name="pk_asset_metrics"),
    )
    # Hypertable where TimescaleDB is present; plain table otherwise (see 0001).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('asset_metrics', 'bucket_start', if_not_exists => TRUE);
            END IF;
        END$$;
        """
    )
    op.create_index("ix_asset_metrics_site", "asset_metrics", ["site_id", "asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_metrics_site", table_name="asset_metrics")
    op.drop_table("asset_metrics")
    op.drop_index("ix_haul_cycles_site_start", table_name="haul_cycles")
    op.drop_table("haul_cycles")
