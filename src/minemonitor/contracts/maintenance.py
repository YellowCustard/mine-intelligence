"""``maintenance.health.v1`` — a derived maintenance health/risk assessment.

This is an **inferred** output, never a measured fact: it carries an explicit ``basis``
(observed | measured | estimated | inferred | unknown), a ``confidence``, and the
``evidence`` behind the risk band. The platform recommends inspection; it never
autonomously shuts a machine down (advisory, brief §15).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["Normal", "Watch", "Elevated", "High", "Critical", "Unknown"]
Basis = Literal["observed", "measured", "estimated", "inferred", "unknown"]


class MaintenanceHealthV1(BaseModel):
    """A derived health/risk assessment for an asset component (maintenance, Phase 5)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["maintenance.health.v1"] = Field(
        default="maintenance.health.v1", alias="schema"
    )
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    component: str = Field(min_length=1)
    ts: datetime
    risk: RiskLevel
    basis: Basis
    confidence: float = Field(ge=0, le=1)
    dimension: str | None = None  # "hours" | "days" | None
    fraction: float | None = None  # interval consumed (1.0 = due)
    remaining: float | None = None  # remaining hours/days to service (labelled by dimension)
    evidence: list[str] = Field(default_factory=list)
    recommended_action: str
    inferred: Literal[True] = True  # never a measured operational fact
