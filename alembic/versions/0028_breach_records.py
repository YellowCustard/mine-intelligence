"""Personal-data breach register (CDPA, SI 155 of 2024; plan C-1).

Additive: two new site-scoped tables. ``breach_records`` holds each breach with its notice
deadlines, fixed when recorded; ``breach_operators`` links the people affected by opaque
operator id, so their personal data stays in ``operators``.

Revision ID: 0028_breach_records
Revises: 0027_vision_models
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_breach_records"
down_revision: str | None = "0027_vision_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "breach_records",
        sa.Column("breach_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), sa.ForeignKey("sites.site_id"), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("data_classes", _JSON, nullable=False),
        sa.Column("notify_individuals", sa.Boolean(), nullable=False),
        sa.Column("regulator_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("individuals_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("regulator_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("regulator_reference", sa.String(), nullable=True),
        sa.Column("individuals_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_by", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(), nullable=True),
        sa.Column("close_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("breach_id"),
    )
    op.create_index("ix_breach_records_site_id", "breach_records", ["site_id"])
    op.create_table(
        "breach_operators",
        sa.Column(
            "breach_id",
            sa.String(),
            sa.ForeignKey("breach_records.breach_id"),
            nullable=False,
        ),
        sa.Column(
            "operator_id",
            sa.String(),
            sa.ForeignKey("operators.operator_id"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("breach_id", "operator_id"),
    )
    op.create_index("ix_breach_operators_operator_id", "breach_operators", ["operator_id"])


def downgrade() -> None:
    op.drop_index("ix_breach_operators_operator_id", table_name="breach_operators")
    op.drop_table("breach_operators")
    op.drop_index("ix_breach_records_site_id", table_name="breach_records")
    op.drop_table("breach_records")
