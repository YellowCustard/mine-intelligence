"""Zone debounce state table and seeded placeholder zones.

Revision ID: 0002_zones_state
Revises: 0001_initial
Create Date: 2026-09-05
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_zones_state"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same PLACEHOLDER anchor and offsets as the simulator, pending the real survey
# (brief §14). Kept in sync so the seeded zones line up with simulated movement.
_BASE_LAT, _BASE_LON = -17.8252, 31.0335
_M_PER_DEG_LAT = 111_320.0


def _center(north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / _M_PER_DEG_LAT
    dlon = east_m / (_M_PER_DEG_LAT * math.cos(math.radians(_BASE_LAT)))
    return _BASE_LAT + dlat, _BASE_LON + dlon


def _box(center_lat: float, center_lon: float, half_m: float) -> dict:
    dlat = half_m / _M_PER_DEG_LAT
    dlon = half_m / (_M_PER_DEG_LAT * math.cos(math.radians(center_lat)))
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [center_lon - dlon, center_lat - dlat],
                [center_lon + dlon, center_lat - dlat],
                [center_lon + dlon, center_lat + dlat],
                [center_lon - dlon, center_lat + dlat],
                [center_lon - dlon, center_lat - dlat],
            ]
        ],
    }


def upgrade() -> None:
    op.create_table(
        "asset_zone_state",
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("zone_id", sa.String(), nullable=False),
        sa.Column("inside", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consec_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consec_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overspeed_consec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overspeed_fired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stationary_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dwell_fired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("site_id", "asset_id", "zone_id", name="pk_asset_zone_state"),
    )

    # Seed PLACEHOLDER zones aligned to the simulator. Rules are data (brief §9).
    face = _center(0, 0)
    rom = _center(600, 200)
    magazine = _center(200, -500)
    haul_mid = _center(300, 100)

    zones = [
        {
            "zone_id": "r1-explosives-magazine",
            "site_id": "kn-zw-01",
            "name": "R1 Explosives Magazine (PLACEHOLDER)",
            "kind": "restricted",
            "geometry": _box(*magazine, 40),
            # No asset class is authorised — any confirmed entry is critical.
            "rules": {"authorized_classes": [], "severity": "critical"},
        },
        {
            "zone_id": "pit-face",
            "site_id": "kn-zw-01",
            "name": "Pit Face — Load (PLACEHOLDER)",
            "kind": "loading",
            "geometry": _box(*face, 70),
            "rules": {},
        },
        {
            "zone_id": "rom-pad",
            "site_id": "kn-zw-01",
            "name": "ROM Pad — Dump (PLACEHOLDER)",
            "kind": "unloading",
            "geometry": _box(*rom, 70),
            "rules": {},
        },
        {
            "zone_id": "haul-road-limit",
            "site_id": "kn-zw-01",
            "name": "Haul Road Speed Limit (PLACEHOLDER)",
            "kind": "speed_limited",
            "geometry": _box(*haul_mid, 130),
            "rules": {"speed_limit_kph": 25, "overspeed_consecutive": 3, "severity": "warning"},
        },
    ]
    op.bulk_insert(
        sa.table(
            "zones",
            sa.column("zone_id", sa.String),
            sa.column("site_id", sa.String),
            sa.column("name", sa.String),
            sa.column("kind", sa.String),
            sa.column("geometry", postgresql.JSONB),
            sa.column("rules", postgresql.JSONB),
        ),
        zones,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM zones WHERE zone_id IN "
        "('r1-explosives-magazine','pit-face','rom-pad','haul-road-limit')"
    )
    op.drop_table("asset_zone_state")
