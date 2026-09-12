"""Optimistic-concurrency version column on incidents (brief §15).

Additive and safe for an existing install: adds one integer column with a server
default of 1 so rows already present get a valid version, then relies on the ORM
to check and bump it on every update. Telemetry and every other table are
untouched.

Revision ID: 0011_incident_version
Revises: 0010_shift_handovers
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_incident_version"
down_revision: str | None = "0010_shift_handovers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column("version_id", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("incidents", "version_id")
