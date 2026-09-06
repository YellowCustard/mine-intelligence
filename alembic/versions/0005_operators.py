"""Operators (personal-data) table and operator foreign keys (compliance, brief §4).

Adds the single home for personal data and wires operator identity onto events
and haul cycles as a foreign key — never a name in a payload. Erasure tombstones
the PII in place, so these FKs stay valid without rewriting operational history.

Revision ID: 0005_operators
Revises: 0004_auth_audit
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_operators"
down_revision: str | None = "0004_auth_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operators",
        sa.Column("operator_id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), sa.ForeignKey("sites.site_id"), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("employee_ref", sa.String(), nullable=True),
        sa.Column("contact", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_operators_site", "operators", ["site_id"])

    # Operator identity as a foreign key on the operational tables.
    op.add_column("events", sa.Column("operator_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_events_operator", "events", "operators", ["operator_id"], ["operator_id"]
    )
    op.create_index("ix_events_operator", "events", ["operator_id"])

    op.add_column("haul_cycles", sa.Column("operator_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_haul_cycles_operator", "haul_cycles", "operators", ["operator_id"], ["operator_id"]
    )
    op.create_index("ix_haul_cycles_operator", "haul_cycles", ["operator_id"])


def downgrade() -> None:
    op.drop_index("ix_haul_cycles_operator", table_name="haul_cycles")
    op.drop_constraint("fk_haul_cycles_operator", "haul_cycles", type_="foreignkey")
    op.drop_column("haul_cycles", "operator_id")

    op.drop_index("ix_events_operator", table_name="events")
    op.drop_constraint("fk_events_operator", "events", type_="foreignkey")
    op.drop_column("events", "operator_id")

    op.drop_index("ix_operators_site", table_name="operators")
    op.drop_table("operators")
