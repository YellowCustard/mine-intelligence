"""Incremental cycle/metric recompute cursors (audit H3/H4).

Additive: one small per-asset bookkeeping table. Existing assets get a row on their next
stored position, and their first run starts a bounded window back from it, so the upgrade
never triggers a whole-history rebuild and leaves derived history older than the window
exactly as it was.

Revision ID: 0028_recompute_cursors
Revises: 0027_vision_models
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_recompute_cursors"
down_revision: str | None = "0027_vision_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recompute_cursors",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("cycles_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_known", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("in_open_visit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metrics_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("through_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dirty_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zones_sig", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("site_id", "asset_id", name="pk_recompute_cursors"),
    )


def downgrade() -> None:
    op.drop_table("recompute_cursors")
