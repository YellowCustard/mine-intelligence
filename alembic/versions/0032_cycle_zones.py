"""Record where each haul cycle loaded and dumped (audit M3).

Additive and safe for an existing install: two nullable columns. Cycles stored before the
upgrade keep NULL until a recompute rebuilds them; an on-demand recompute fills them in
for every cycle whose positions retention still holds.

Revision ID: 0032_cycle_zones
Revises: 0031_source_status
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_cycle_zones"
down_revision: str | None = "0031_source_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("haul_cycles", sa.Column("load_zone_id", sa.String(), nullable=True))
    op.add_column("haul_cycles", sa.Column("dump_zone_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("haul_cycles", "dump_zone_id")
    op.drop_column("haul_cycles", "load_zone_id")
