"""Incident lifecycle and downtime/delay classification (operations platform).

Additive and safe for an existing install: creates three new annotation tables and
touches nothing else. Raw telemetry (``positions``) is untouched, so derived
analytics stay reproducible; incidents link to alarms without mutating them.

Revision ID: 0009_incidents_delays
Revises: 0008_shift_definitions
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_incidents_delays"
down_revision: str | None = "0008_shift_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), sa.ForeignKey("events.event_id"), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("assignee", sa.String(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_category", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_incidents_site", "incidents", ["site_id"])
    op.create_index("ix_incidents_event", "incidents", ["event_id"])
    op.create_index("ix_incidents_site_state", "incidents", ["site_id", "state"])

    op.create_table(
        "incident_notes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "incident_id", sa.String(), sa.ForeignKey("incidents.incident_id"), nullable=False
        ),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("from_state", sa.String(), nullable=True),
        sa.Column("to_state", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
    )
    op.create_index("ix_incident_notes_site", "incident_notes", ["site_id"])
    op.create_index("ix_incident_notes_incident", "incident_notes", ["incident_id", "ts"])

    op.create_table(
        "delay_classifications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delay_classifications_site", "delay_classifications", ["site_id"])
    op.create_index(
        "ix_delay_classifications_site_start", "delay_classifications", ["site_id", "start_ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_delay_classifications_site_start", table_name="delay_classifications")
    op.drop_index("ix_delay_classifications_site", table_name="delay_classifications")
    op.drop_table("delay_classifications")

    op.drop_index("ix_incident_notes_incident", table_name="incident_notes")
    op.drop_index("ix_incident_notes_site", table_name="incident_notes")
    op.drop_table("incident_notes")

    op.drop_index("ix_incidents_site_state", table_name="incidents")
    op.drop_index("ix_incidents_event", table_name="incidents")
    op.drop_index("ix_incidents_site", table_name="incidents")
    op.drop_table("incidents")
