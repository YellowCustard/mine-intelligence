"""Initial schema: sites, assets, zones, positions (hypertable), events.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Placeholder site coordinates — NOT a real survey. Replace with the site survey
# polygons when they arrive (brief §14).
_PLACEHOLDER_SITE = "kn-zw-01"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    op.create_table(
        "sites",
        sa.Column("site_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False, server_default="Africa/Harare"),
    )

    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_class", sa.String(), nullable=False, server_default="generic"),
        sa.Column("make", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("asset_id", "site_id"),
    )
    op.create_index("ix_assets_site_id", "assets", ["site_id"])

    op.create_table(
        "zones",
        sa.Column("zone_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="generic"),
        sa.Column("geometry", postgresql.JSONB(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("zone_id", "site_id"),
    )
    op.create_index("ix_zones_site_id", "zones", ["site_id"])

    op.create_table(
        "positions",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("altitude_m", sa.Float(), nullable=True),
        sa.Column("speed_kph", sa.Float(), nullable=True),
        sa.Column("heading_deg", sa.Float(), nullable=True),
        sa.Column("hdop", sa.Float(), nullable=True),
        sa.Column("satellites", sa.Integer(), nullable=True),
        sa.Column("ignition", sa.Boolean(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("site_id", "asset_id", "ts", name="pk_positions"),
    )
    # Convert positions into a TimescaleDB hypertable partitioned on ts.
    op.execute("SELECT create_hypertable('positions', 'ts', if_not_exists => TRUE)")
    op.create_index("ix_positions_site_asset", "positions", ["site_id", "asset_id"])

    op.create_table(
        "events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("advisory", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("acknowledged_by", sa.String(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_events_site_id", "events", ["site_id"])

    # Seed one placeholder site and a few assets so the API has data to serve.
    # Coordinates/fleet are placeholders pending the site survey (brief §14).
    op.bulk_insert(
        sa.table(
            "sites",
            sa.column("site_id", sa.String),
            sa.column("name", sa.String),
            sa.column("timezone", sa.String),
        ),
        [
            {
                "site_id": _PLACEHOLDER_SITE,
                "name": "Kanyemba Gold (PLACEHOLDER — pending survey)",
                "timezone": "Africa/Harare",
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "assets",
            sa.column("asset_id", sa.String),
            sa.column("site_id", sa.String),
            sa.column("asset_class", sa.String),
        ),
        [
            {"asset_id": "HT-101", "site_id": _PLACEHOLDER_SITE, "asset_class": "haul_truck"},
            {"asset_id": "HT-102", "site_id": _PLACEHOLDER_SITE, "asset_class": "haul_truck"},
            {"asset_id": "EX-01", "site_id": _PLACEHOLDER_SITE, "asset_class": "excavator"},
            {"asset_id": "LV-07", "site_id": _PLACEHOLDER_SITE, "asset_class": "light_vehicle"},
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_events_site_id", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_positions_site_asset", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_zones_site_id", table_name="zones")
    op.drop_table("zones")
    op.drop_index("ix_assets_site_id", table_name="assets")
    op.drop_table("assets")
    op.drop_table("sites")
