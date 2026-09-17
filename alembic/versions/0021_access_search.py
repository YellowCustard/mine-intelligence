"""Access-control slice 3 (FP-07): metal-detector result on access events.

Additive: one nullable column recording the metal-detector outcome of a physical search
(the detector's association with a gate passage). Random-search selection reuses the existing
``search_selected``; missed-search escalation reuses ``search_completed`` — no schema for those.

Revision ID: 0021_access_search
Revises: 0020_access_events
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_access_search"
down_revision: str | None = "0020_access_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("access_events", sa.Column("metal_detected", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("access_events", "metal_detected")
