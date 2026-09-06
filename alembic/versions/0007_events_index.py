"""Composite index on events(site_id, ts) for the alarm-queue scan (Phase 3).

The dashboard reads "this site's events, newest first" on every poll; without a
composite index that ordered scan hits the heap and sorts unindexed.

Revision ID: 0007_events_index
Revises: 0006_lockout_heartbeat
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_events_index"
down_revision: str | None = "0006_lockout_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_events_site_ts", "events", ["site_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_events_site_ts", table_name="events")
