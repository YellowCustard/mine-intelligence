"""First-party vision core (Stage B — docs/VISION_BUILD_GATE.md §4).

Additive and safe for an existing install: six new tables and one nullable column.
``vision_rule_versions`` (immutable rule configs), ``vision_operational_events`` (derived,
recomputable conclusions), ``vision_observations`` (per-frame perception, lab/pulled slices —
a TimescaleDB hypertable when the extension is present), ``vision_node_health`` and
``vision_camera_health`` (edge/camera health), ``vision_zone_rollups`` (per camera × zone ×
bucket counts, the reporting source), and ``cameras.image_zones`` (scene config). No table
can hold identity, a face, or frame bytes.

Revision ID: 0026_vision_core
Revises: 0025_material_measurements
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_vision_core"
down_revision: str | None = "0025_material_measurements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("cameras", sa.Column("image_zones", _JSON, nullable=True))

    op.create_table(
        "vision_rule_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("rule_version", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("parameters", _JSON, nullable=False),
        sa.Column("zone_version", sa.String(), nullable=True),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "rule_id", "rule_version", name="uq_vision_rule_version"),
    )
    op.create_index("ix_vision_rule_versions_site", "vision_rule_versions", ["site_id"])

    op.create_table(
        "vision_operational_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("camera_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("rule_version", sa.String(), nullable=False),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("zone_version", sa.String(), nullable=True),
        sa.Column("subject", _JSON, nullable=True),
        sa.Column("evidence", _JSON, nullable=False),
        sa.Column("promoted_event_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_vision_operational_events_site_ts", "vision_operational_events", ["site_id", "ts"]
    )
    op.create_index(
        "ix_vision_operational_events_site_camera",
        "vision_operational_events",
        ["site_id", "camera_id"],
    )

    op.create_table(
        "vision_observations",
        sa.Column("observation_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("camera_id", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frame_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("image", _JSON, nullable=True),
        sa.Column("calibration_version", sa.String(), nullable=True),
        sa.Column("objects", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("observation_id", "ts", name="pk_vision_observations"),
    )
    # Hypertable on ts when TimescaleDB is installed (the PK includes the partition column).
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                    PERFORM create_hypertable('vision_observations', 'ts', if_not_exists => TRUE);
                END IF;
            END$$;
            """
        )
    op.create_index(
        "ix_vision_observations_site_camera_ts",
        "vision_observations",
        ["site_id", "camera_id", "ts"],
    )

    op.create_table(
        "vision_node_health",
        sa.Column("health_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("cameras_active", sa.Integer(), nullable=False),
        sa.Column("fps_mean", sa.Float(), nullable=False),
        sa.Column("queue_depth", sa.Integer(), nullable=False),
        sa.Column("dropped_frames", sa.Integer(), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column("gpu_pct", sa.Float(), nullable=True),
        sa.Column("model_ids", _JSON, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("health_id"),
    )
    op.create_index(
        "ix_vision_node_health_site_node_ts", "vision_node_health", ["site_id", "node_id", "ts"]
    )

    op.create_table(
        "vision_camera_health",
        sa.Column("health_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("camera_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("blur_score", sa.Float(), nullable=True),
        sa.Column("luma_mean", sa.Float(), nullable=True),
        sa.Column("frozen_frames", sa.Integer(), nullable=True),
        sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("health_id"),
    )
    op.create_index(
        "ix_vision_camera_health_site_camera_ts",
        "vision_camera_health",
        ["site_id", "camera_id", "ts"],
    )

    op.create_table(
        "vision_zone_rollups",
        sa.Column("rollup_id", sa.String(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("camera_id", sa.String(), nullable=False),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_s", sa.Integer(), nullable=False),
        sa.Column("object_class", sa.String(), nullable=False),
        sa.Column("count_max", sa.Integer(), nullable=False),
        sa.Column("count_mean", sa.Float(), nullable=False),
        sa.Column("entries", sa.Integer(), nullable=False),
        sa.Column("exits", sa.Integer(), nullable=False),
        sa.Column("dwell_s_max", sa.Float(), nullable=False),
        sa.Column("dwell_s_p50", sa.Float(), nullable=True),
        sa.Column("observed_s", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("rollup_id"),
    )
    op.create_index("ix_vision_zone_rollups_site_ts", "vision_zone_rollups", ["site_id", "ts"])
    op.create_index(
        "ix_vision_zone_rollups_site_zone_ts", "vision_zone_rollups", ["site_id", "zone_id", "ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_vision_zone_rollups_site_zone_ts", table_name="vision_zone_rollups")
    op.drop_index("ix_vision_zone_rollups_site_ts", table_name="vision_zone_rollups")
    op.drop_table("vision_zone_rollups")
    op.drop_index("ix_vision_camera_health_site_camera_ts", table_name="vision_camera_health")
    op.drop_table("vision_camera_health")
    op.drop_index("ix_vision_node_health_site_node_ts", table_name="vision_node_health")
    op.drop_table("vision_node_health")
    op.drop_index("ix_vision_observations_site_camera_ts", table_name="vision_observations")
    op.drop_table("vision_observations")
    op.drop_index(
        "ix_vision_operational_events_site_camera", table_name="vision_operational_events"
    )
    op.drop_index("ix_vision_operational_events_site_ts", table_name="vision_operational_events")
    op.drop_table("vision_operational_events")
    op.drop_index("ix_vision_rule_versions_site", table_name="vision_rule_versions")
    op.drop_table("vision_rule_versions")
    op.drop_column("cameras", "image_zones")
