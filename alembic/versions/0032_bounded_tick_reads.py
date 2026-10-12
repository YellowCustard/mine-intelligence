"""Tick reads that do not grow with history (soak).

Additive and safe for an existing install: one nullable column and two indexes.

- ``access_events.search_escalated_at`` marks a passage whose skipped search has been raised,
  so the tick reads only searches still pending (a partial index holds just those) instead of
  every missed search ever, and an alarm purged by events retention is not raised again.
  Passages already escalated are marked from their ``search_missed`` alarms.
- ``events (site_id, type, ts)``: the occupancy check's "latest alarm of this type for this
  zone" read the site's whole alarm history backwards whenever the zone had none.

Revision ID: 0032_bounded_tick_reads
Revises: 0031_source_status
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_bounded_tick_reads"
down_revision: str | None = "0031_source_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tick's query repeats this predicate, which is what lets the planner use the index.
_PENDING = (
    "search_selected IS TRUE AND search_completed IS NOT TRUE AND search_escalated_at IS NULL"
)


def upgrade() -> None:
    op.add_column(
        "access_events", sa.Column("search_escalated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """
        UPDATE access_events SET search_escalated_at = (
            SELECT e.ts FROM events e WHERE e.event_id = 'search-missed-' || access_events.id
        )
        WHERE search_selected IS TRUE AND EXISTS (
            SELECT 1 FROM events e WHERE e.event_id = 'search-missed-' || access_events.id
        )
        """
    )
    op.create_index(
        "ix_access_events_search_pending",
        "access_events",
        ["site_id", "ts"],
        postgresql_where=sa.text(_PENDING),
        sqlite_where=sa.text(_PENDING),
    )
    op.create_index("ix_events_site_type_ts", "events", ["site_id", "type", "ts"])


def downgrade() -> None:
    op.drop_index("ix_events_site_type_ts", table_name="events")
    op.drop_index("ix_access_events_search_pending", table_name="access_events")
    op.drop_column("access_events", "search_escalated_at")
