"""Per-site configurable shift definitions (operations platform).

Makes the shift a first-class, configurable operational unit. Additive and safe
for an existing install: creates one table and seeds day/night defaults for any
site already present. Shift *instances* remain derived, never stored.

Revision ID: 0008_shift_definitions
Revises: 0007_events_index
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_shift_definitions"
down_revision: str | None = "0007_events_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_definitions",
        sa.Column("site_id", sa.String(), sa.ForeignKey("sites.site_id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("start_hour_local", sa.Integer(), nullable=False),
        sa.Column("duration_hours", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("site_id", "name", name="pk_shift_definitions"),
    )
    op.create_index("ix_shift_definitions_site", "shift_definitions", ["site_id"])

    # Seed the conventional two 12-hour shifts (day 06:00, night 18:00) for every
    # existing site. The resolver falls back to these defaults anyway, so this is
    # a convenience that makes the config visible and editable.
    op.execute(
        "INSERT INTO shift_definitions (site_id, name, start_hour_local, duration_hours, enabled) "
        "SELECT site_id, 'day', 6, 12, TRUE FROM sites"
    )
    op.execute(
        "INSERT INTO shift_definitions (site_id, name, start_hour_local, duration_hours, enabled) "
        "SELECT site_id, 'night', 18, 12, TRUE FROM sites"
    )


def downgrade() -> None:
    op.drop_index("ix_shift_definitions_site", table_name="shift_definitions")
    op.drop_table("shift_definitions")
