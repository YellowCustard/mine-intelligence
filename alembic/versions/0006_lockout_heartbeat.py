"""Auth lockout and service heartbeat tables (ops readiness & auth hardening).

Crash-safe failed-login state (brief §3) and a background-worker liveness beat
that ``/health`` and the ingestor's container healthcheck read.

Revision ID: 0006_lockout_heartbeat
Revises: 0005_operators
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_lockout_heartbeat"
down_revision: str | None = "0005_operators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_lockout",
        sa.Column("username", sa.String(), primary_key=True),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "service_heartbeat",
        sa.Column("service", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeat")
    op.drop_table("auth_lockout")
