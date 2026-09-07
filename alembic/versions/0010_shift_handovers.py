"""Shift handovers (operations platform).

Additive and safe for an existing install: creates one annotation table and
touches nothing else. Telemetry is untouched; the handover's stored summary is a
point-in-time snapshot, while the live scorecard stays recomputable.

Revision ID: 0010_shift_handovers
Revises: 0009_incidents_delays
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on PostgreSQL (production); portable JSON elsewhere — matches the model.
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

revision: str = "0010_shift_handovers"
down_revision: str | None = "0009_incidents_delays"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_handovers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("shift_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("summary", _JSON, nullable=False),
        sa.Column("outgoing_by", sa.String(), nullable=False),
        sa.Column("outgoing_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("incoming_by", sa.String(), nullable=True),
        sa.Column("incoming_notes", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_shift_handovers_site", "shift_handovers", ["site_id"])
    op.create_index("ix_shift_handovers_site_shift", "shift_handovers", ["site_id", "shift_id"])


def downgrade() -> None:
    op.drop_index("ix_shift_handovers_site_shift", table_name="shift_handovers")
    op.drop_index("ix_shift_handovers_site", table_name="shift_handovers")
    op.drop_table("shift_handovers")
