"""``asset.metrics.v1`` — per-asset rollups (contract published in M1, computed in M4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetMetricsV1(BaseModel):
    """A per-asset rollup over a fixed time bucket.

    Derived from positions, recomputable, never hand-edited. The contract ships
    in M1; the computation and storage arrive in M4.
    """

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["asset.metrics.v1"] = Field(default="asset.metrics.v1", alias="schema")
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    bucket_start: datetime
    bucket_end: datetime
    distance_m: float | None = Field(default=None, ge=0)
    moving_time_s: float | None = Field(default=None, ge=0)
    idle_time_s: float | None = Field(default=None, ge=0)
    max_speed_kph: float | None = Field(default=None, ge=0)
    mean_speed_kph: float | None = Field(default=None, ge=0)
    zone_dwell_s: dict[str, float] | None = None
    loads_completed: int | None = Field(default=None, ge=0)
