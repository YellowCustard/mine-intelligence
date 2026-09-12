"""Maintenance domain — service plans and work orders (Phase 5, deterministic).

Additive and safe for an existing install: two new tables only. Work orders are
operational annotations; health/risk is derived on read, not stored.

Revision ID: 0017_maintenance
Revises: 0016_weighbridge
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_maintenance"
down_revision: str | None = "0016_weighbridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_plans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("component", sa.String(), nullable=False),
        sa.Column("interval_hours", sa.Float(), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "site_id", "asset_id", "component", name="uq_maint_plan_asset_component"
        ),
    )
    op.create_index("ix_maint_plans_site", "maintenance_plans", ["site_id"])

    op.create_table(
        "work_orders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("component", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("at_engine_hours", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_orders_site_asset", "work_orders", ["site_id", "asset_id"])


def downgrade() -> None:
    op.drop_index("ix_work_orders_site_asset", table_name="work_orders")
    op.drop_table("work_orders")
    op.drop_index("ix_maint_plans_site", table_name="maintenance_plans")
    op.drop_table("maintenance_plans")
