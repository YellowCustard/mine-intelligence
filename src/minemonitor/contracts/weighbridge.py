"""``weighbridge.transaction.v1`` — a measured weighbridge ticket.

Manufacturer-neutral: any scale or adapter (REST, CSV, DB, serial gateway) maps onto
this one shape. Gross/tare/net are the measured figures the bridge prints. Net
consistency (net ≈ gross − tare) is flagged downstream, never silently corrected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WeighDirection = Literal["inbound", "outbound"]
WeighSource = Literal["manual", "import", "api", "serial"]


class WeighbridgeTransactionV1(BaseModel):
    """A measured weighbridge ticket (weighbridge domain, Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["weighbridge.transaction.v1"] = Field(
        default="weighbridge.transaction.v1", alias="schema"
    )
    ticket_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    ticket_no: str = Field(min_length=1)
    ts: datetime
    direction: WeighDirection = "outbound"
    gross_kg: float = Field(ge=0)
    tare_kg: float = Field(ge=0)
    net_kg: float
    scale_id: str | None = None
    asset_id: str | None = None
    trailer: str | None = None
    material: str | None = None
    destination: str | None = None
    customer: str | None = None
    source: WeighSource = "manual"
    measured: Literal[True] = True
