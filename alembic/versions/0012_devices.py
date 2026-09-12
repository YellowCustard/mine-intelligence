"""Device provisioning table (brief §10/§11).

Additive and safe for an existing install: one new table binding a device identity
to exactly one asset. Telemetry and every other table are untouched; ingest keeps
working unchanged until the registered-device requirement is turned on.

Revision ID: 0012_devices
Revises: 0011_incident_version
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_devices"
down_revision: str | None = "0011_incident_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("device_id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("expected_interval_s", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("site_id", "asset_id", name="uq_devices_site_asset"),
    )
    op.create_index("ix_devices_site", "devices", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_devices_site", table_name="devices")
    op.drop_table("devices")
