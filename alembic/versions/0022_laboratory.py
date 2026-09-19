"""Laboratory data ingestion (FP-08): immutable results + append-only corrections.

Additive and safe for an existing install: two new tables only. ``laboratory_results`` holds
the measured assay result with the raw original stored write-once and hashed (tamper-evidence);
``laboratory_corrections`` is append-only and references a result without mutating it.

Revision ID: 0022_laboratory
Revises: 0021_access_search
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022_laboratory"
down_revision: str | None = "0021_access_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "laboratory_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("instrument", sa.String(), nullable=False),
        sa.Column("sample_ref", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("element", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=True),
        sa.Column("batch_ref", sa.String(), nullable=True),
        sa.Column("original", _JSON, nullable=False),
        sa.Column("original_hash", sa.String(), nullable=False),
        sa.Column("original_file_ref", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="spectraa"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_laboratory_results_site", "laboratory_results", ["site_id"])
    op.create_index("ix_laboratory_results_site_ts", "laboratory_results", ["site_id", "ts"])
    op.create_table(
        "laboratory_corrections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("result_id", sa.String(), nullable=False),
        sa.Column("corrected_value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.ForeignKeyConstraint(["result_id"], ["laboratory_results.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_laboratory_corrections_site", "laboratory_corrections", ["site_id"])
    op.create_index(
        "ix_laboratory_corrections_result", "laboratory_corrections", ["result_id", "ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_laboratory_corrections_result", table_name="laboratory_corrections")
    op.drop_index("ix_laboratory_corrections_site", table_name="laboratory_corrections")
    op.drop_table("laboratory_corrections")
    op.drop_index("ix_laboratory_results_site_ts", table_name="laboratory_results")
    op.drop_index("ix_laboratory_results_site", table_name="laboratory_results")
    op.drop_table("laboratory_results")
