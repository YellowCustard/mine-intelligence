"""``dispatch.recommendation.v1`` — an advisory truck→job dispatch recommendation.

Decision-support only. A recommendation is a **suggestion** a supervisor must approve
before it becomes a dispatched instruction; ``advisory`` is always ``True`` and the
platform never actuates a machine (brief §15). ``rationale`` carries the evidence, and
``score`` is a transparent heuristic rank — never a claim of optimality.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DispatchRecommendationV1(BaseModel):
    """An advisory dispatch recommendation (dispatch domain, Phase 6)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["dispatch.recommendation.v1"] = Field(
        default="dispatch.recommendation.v1", alias="schema"
    )
    assignment_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    ts: datetime
    objective: str = "balanced"
    score: float
    rationale: list[str] = Field(default_factory=list)
    advisory: Literal[True] = True  # a suggestion, not an instruction; needs approval
