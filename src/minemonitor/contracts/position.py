"""``asset.position.v1`` — raw GNSS telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetPositionV1(BaseModel):
    """A single position fix from a machine.

    ``ts`` is device time; ``received_at`` is server time. The gap between them
    is how buffered backfill is detected and corrected.
    """

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["asset.position.v1"] = Field(default="asset.position.v1", alias="schema")
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    ts: datetime
    received_at: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float | None = None
    speed_kph: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    hdop: float | None = Field(default=None, ge=0)
    satellites: int | None = Field(default=None, ge=0)
    ignition: bool | None = None
    source: str | None = None
