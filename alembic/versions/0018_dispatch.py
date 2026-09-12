"""Dispatch domain — jobs and assignments (Phase 6, decision-support).

Additive and safe for an existing install: two new tables only. Assignments are
operational annotations (advisory recommendations + approved decisions), stored
separately from telemetry. The platform never actuates machinery.

Revision ID: 0018_dispatch
Revises: 0017_maintenance
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_dispatch"
down_revision: str | None = "0017_maintenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "dispatch_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("material", sa.String(), nullable=True),
        sa.Column("source_zone", sa.String(), nullable=True),
        sa.Column("dest_zone", sa.String(), nullable=True),
        sa.Column("target_trucks", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dispatch_jobs_site_status", "dispatch_jobs", ["site_id", "status"])

    op.create_table(
        "dispatch_assignments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("objective", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rationale", _JSON, nullable=False),
        sa.Column("recommended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.ForeignKeyConstraint(["job_id"], ["dispatch_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dispatch_assignments_site_state", "dispatch_assignments", ["site_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_dispatch_assignments_site_state", table_name="dispatch_assignments")
    op.drop_table("dispatch_assignments")
    op.drop_index("ix_dispatch_jobs_site_status", table_name="dispatch_jobs")
    op.drop_table("dispatch_jobs")
