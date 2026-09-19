"""Vision NVR ingestion, Stage A (VISION_BUILD_GATE §4.5): normalised vendor events.

Additive and safe for an existing install: one new table. ``vision_vendor_events`` stores the
Dahua NVR's AI events normalised to ``vision.vendor_event.v1`` (vendor-inferred provenance),
the basis for an ``event.v1`` promotion. No identity/biometric field is ever stored.

Revision ID: 0023_vision_vendor_events
Revises: 0022_laboratory
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_vision_vendor_events"
down_revision: str | None = "0022_laboratory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vision_vendor_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("source_system", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=False),
        sa.Column("camera_id", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("vendor_type", sa.String(), nullable=False),
        sa.Column("normalized_type", sa.String(), nullable=False),
        sa.Column("vendor_confidence", sa.Float(), nullable=True),
        sa.Column("vendor_rule_name", sa.String(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clip_ref", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vision_vendor_events_site", "vision_vendor_events", ["site_id"])
    op.create_index("ix_vision_vendor_events_site_ts", "vision_vendor_events", ["site_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_vision_vendor_events_site_ts", table_name="vision_vendor_events")
    op.drop_index("ix_vision_vendor_events_site", table_name="vision_vendor_events")
    op.drop_table("vision_vendor_events")
