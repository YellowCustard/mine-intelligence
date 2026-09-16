"""Access-control integration (FP-07): access_events table + operator access status.

Additive and safe for an existing install: one new table, plus two non-null status columns
on ``operators`` with server defaults so existing rows behave as "inducted, not suspended".

Revision ID: 0020_access_events
Revises: 0019_cameras
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_access_events"
down_revision: str | None = "0019_cameras"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operators",
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "operators",
        sa.Column("inducted", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "access_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_system", sa.String(), nullable=False),
        sa.Column("gate_id", sa.String(), nullable=False),
        sa.Column("credential_ref", sa.String(), nullable=True),
        sa.Column("operator_ref", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("search_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("search_completed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_events_site_ts", "access_events", ["site_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_access_events_site_ts", table_name="access_events")
    op.drop_table("access_events")
    op.drop_column("operators", "inducted")
    op.drop_column("operators", "suspended")
