"""Shift report outbox: one row per (shift, channel, recipient).

Additive and safe for an existing install: one new table. The maintenance tick queues each
ended shift's report here and a dispatcher sends it with retry, so a report survives an
outage or a power cut and goes to each recipient once.

Revision ID: 0033_shift_report_deliveries
Revises: 0032_cycle_zones
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_shift_report_deliveries"
down_revision: str | None = "0032_cycle_zones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_report_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("shift_id", sa.String(), nullable=False),
        sa.Column("shift_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "site_id", "shift_id", "channel", "target", name="uq_shift_report_deliveries"
        ),
    )
    op.create_index("ix_shift_report_deliveries_site_id", "shift_report_deliveries", ["site_id"])
    op.create_index(
        "ix_shift_report_deliveries_state_next",
        "shift_report_deliveries",
        ["state", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_shift_report_deliveries_state_next", table_name="shift_report_deliveries")
    op.drop_index("ix_shift_report_deliveries_site_id", table_name="shift_report_deliveries")
    op.drop_table("shift_report_deliveries")
