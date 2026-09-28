"""Vision model registry (docs/VISION_ARCHITECTURE.md §6).

Additive: one new global table. Each row pins a model version to its exact weights (SHA-256),
licence, training data and measured metrics, with a lifecycle status. Observations cite
``model.id``/``model.version``; this table is what that resolves to.

Revision ID: 0027_vision_models
Revises: 0026_vision_core
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_vision_models"
down_revision: str | None = "0026_vision_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "vision_models",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("architecture", sa.String(), nullable=True),
        sa.Column("licence", sa.String(), nullable=False),
        sa.Column("weights_sha256", sa.String(), nullable=False),
        sa.Column("weights_ref", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("training_dataset", sa.String(), nullable=True),
        sa.Column("classes", _JSON, nullable=False),
        sa.Column("input_size", _JSON, nullable=True),
        sa.Column("metrics", _JSON, nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="experimental"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "version", name="uq_vision_model_version"),
    )


def downgrade() -> None:
    op.drop_table("vision_models")
