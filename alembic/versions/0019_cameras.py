"""Camera estate & AI readiness (Mine Monitor Vision, FP-01 / RAN Mines Slice 1).

Additive and safe for an existing install: one new table only. Cameras are reference/config
data (no video, no telemetry, no events) capturing the Phase 0 camera audit and AI-readiness
metadata. ``stream_url`` is a secret and is never returned by the API.

Revision ID: 0019_cameras
Revises: 0018_dispatch
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019_cameras"
down_revision: str | None = "0018_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "cameras",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("location_description", sa.String(), nullable=True),
        sa.Column("make_model", sa.String(), nullable=True),
        sa.Column("stream_url", sa.String(), nullable=True),
        sa.Column("stream_type", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("stream_kind", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("fps", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(), nullable=True),
        sa.Column("lighting", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("has_usable_ai_stream", sa.Boolean(), nullable=True),
        sa.Column("ai_suitability", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("blind_spot_notes", sa.Text(), nullable=True),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("homography", _JSON, nullable=True),
        sa.Column("calibration_status", sa.String(), nullable=False, server_default="none"),
        sa.Column("model_deployed", sa.String(), nullable=True),
        sa.Column("model_version", sa.String(), nullable=True),
        sa.Column("health_state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cameras_site", "cameras", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_cameras_site", table_name="cameras")
    op.drop_table("cameras")
