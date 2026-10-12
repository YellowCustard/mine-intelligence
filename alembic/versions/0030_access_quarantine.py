"""Gate clock-skew quarantine (plan G1): access events refused for a future device time.

Additive and safe for an existing install: one new table. A gate event dated beyond
``MM_MAX_FUTURE_SKEW_S`` is no longer stored as an access event (it moved the gate poller's
cursor into the future and hid every later event); it lands here instead and raises one
``source_clock_skew`` alarm per gate. No personal data: no operator or credential reference.

Revision ID: 0030_access_quarantine
Revises: 0029_recompute_cursors
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_access_quarantine"
down_revision: str | None = "0029_recompute_cursors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_quarantine",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("source_system", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=False),
        sa.Column("gate_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("claimed_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("skew_s", sa.Float(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_quarantine_site_id", "access_quarantine", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_access_quarantine_site_id", table_name="access_quarantine")
    op.drop_table("access_quarantine")
