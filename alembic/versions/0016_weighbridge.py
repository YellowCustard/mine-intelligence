"""Weighbridge domain — scales and weigh tickets (Phase 4).

Additive and safe for an existing install: two new tables only. Weigh tickets are
measured observations, stored separately from telemetry and annotations. ``ticket_no``
is unique per site so re-imports are idempotent.

Revision ID: 0016_weighbridge
Revises: 0015_fuel
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_weighbridge"
down_revision: str | None = "0015_fuel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weighbridges",
        sa.Column("scale_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("scale_id"),
    )
    op.create_index("ix_weighbridges_site", "weighbridges", ["site_id"])

    op.create_table(
        "weigh_tickets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("ticket_no", sa.String(), nullable=False),
        sa.Column("scale_id", sa.String(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("gross_kg", sa.Float(), nullable=False),
        sa.Column("tare_kg", sa.Float(), nullable=False),
        sa.Column("net_kg", sa.Float(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("trailer", sa.String(), nullable=True),
        sa.Column("material", sa.String(), nullable=True),
        sa.Column("destination", sa.String(), nullable=True),
        sa.Column("customer", sa.String(), nullable=True),
        sa.Column("operator_ref", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "ticket_no", name="uq_weigh_tickets_site_ticket"),
    )
    op.create_index("ix_weigh_tickets_site_ts", "weigh_tickets", ["site_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_weigh_tickets_site_ts", table_name="weigh_tickets")
    op.drop_table("weigh_tickets")
    op.drop_index("ix_weighbridges_site", table_name="weighbridges")
    op.drop_table("weighbridges")
