"""Record which loader each haul cycle loaded at (loader and truck pairing).

Additive and safe for an existing install: one nullable column. Cycles stored before the
upgrade keep NULL until a recompute rebuilds them; an on-demand recompute pairs every
cycle whose truck and loader positions retention still holds.

Revision ID: 0035_cycle_loader
Revises: 0034_bounded_tick_reads
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_cycle_loader"
down_revision: str | None = "0034_bounded_tick_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("haul_cycles", sa.Column("loader_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("haul_cycles", "loader_id")
