"""``fuel.transaction.v1`` — a measured fuel movement.

Emitted when a fuel dispense or tank delivery is recorded. Litres and the optional
odometer/engine-hour readings are measured facts; efficiency derived from them is
calculated separately and labelled as such (never presented as measured).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FuelDirection = Literal["dispense", "delivery"]
FuelSource = Literal["manual", "dispenser", "tank_meter", "import"]


class FuelTransactionV1(BaseModel):
    """A measured fuel transaction (fuel domain, Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["fuel.transaction.v1"] = Field(default="fuel.transaction.v1", alias="schema")
    transaction_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    ts: datetime
    litres: float = Field(gt=0)
    direction: FuelDirection = "dispense"
    source: FuelSource = "manual"
    asset_id: str | None = None
    tank_id: str | None = None
    station: str | None = None
    odometer_km: float | None = Field(default=None, ge=0)
    engine_hours: float | None = Field(default=None, ge=0)
    measured: Literal[True] = True  # this event carries a measured quantity, not an estimate
