"""Per-device broker credential hash (MQTT broker-auth enforcement, brief §10/§11).

Additive and safe for an existing install: adds one nullable column that holds the
Mosquitto ``$7$`` hash of a device's broker password. Existing devices get NULL and
are simply absent from the generated broker password file until a secret is issued
(``POST /sites/{id}/devices/{device_id}/rotate-secret``). Telemetry and every other
table are untouched.

Revision ID: 0013_device_broker_pw
Revises: 0012_devices
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_device_broker_pw"
down_revision: str | None = "0012_devices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("broker_pw_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "broker_pw_hash")
