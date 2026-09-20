"""Gold-reconciliation groundwork (FP-09): measured material measurements.

Additive and safe for an existing install: one new table. ``material_measurements`` stores
measured quantities through the gold circuit (ore→…→gate) with source provenance — the
ingestion layer the later expected-vs-observed reconciliation slices build on. This table holds
only measured inputs; expectations/variance/exceptions are deferred.

Revision ID: 0025_material_measurements
Revises: 0024_generator_readings
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_material_measurements"
down_revision: str | None = "0024_generator_readings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_measurements",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("assay_ref", sa.String(), nullable=True),
        sa.Column("quality", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_material_measurements_site", "material_measurements", ["site_id"])
    op.create_index(
        "ix_material_measurements_site_stage_ts",
        "material_measurements",
        ["site_id", "stage", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_measurements_site_stage_ts", table_name="material_measurements")
    op.drop_index("ix_material_measurements_site", table_name="material_measurements")
    op.drop_table("material_measurements")
