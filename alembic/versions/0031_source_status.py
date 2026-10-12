"""Polled-feed reachability (plan G2): one row per (site, source).

Additive and safe for an existing install: one new table. It records when the current outage of
a polled feed (the Dahua gate API) began, so a ``source_offline`` alarm survives an ingestor
restart and fires once per outage.

Revision ID: 0031_source_status
Revises: 0030_access_quarantine
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_source_status"
down_revision: str | None = "0030_access_quarantine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_status",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("site_id", "source"),
    )


def downgrade() -> None:
    op.drop_table("source_status")
