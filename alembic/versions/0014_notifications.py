"""Notification outbox (store-and-forward alert egress, brief §3/§15).

Additive and safe for an existing install: a new table only, no change to any
existing one. Telemetry and the alarm queue are untouched — a notification links to
its event by ``event_id`` and never mutates it.

Revision ID: 0014_notifications
Revises: 0013_device_broker_pw
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_notifications"
down_revision: str | None = "0013_device_broker_pw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "channel", "target", name="uq_notifications_event_channel"),
    )
    op.create_index("ix_notifications_site", "notifications", ["site_id"])
    op.create_index("ix_notifications_state_next", "notifications", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_state_next", table_name="notifications")
    op.drop_index("ix_notifications_site", table_name="notifications")
    op.drop_table("notifications")
