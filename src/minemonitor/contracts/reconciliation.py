"""``material.measurement.v1`` — gold-reconciliation measurement ingestion (FP-09 groundwork).

Continuous gold reconciliation compares expected vs observed material through the circuit
(ore→crusher→mill→tanks→carbon→furnace→output→gate) and surfaces *unexplained variance* as an
exception — it **never concludes theft**; the operational conclusion is a human decision. This
groundwork is only the **measurement-ingestion** layer: each measured quantity is recorded with
its stage, unit and source provenance, so the later expected-vs-observed slices are reproducible
from stored measurements. Expectations, tolerance bands and ``reconciliation.exception.v1`` are
deferred (they need weightometer feeds + baselined historical variation). This contract carries
only **measured** inputs — never an expectation or a variance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MaterialStage = Literal["ore", "crusher", "mill", "tanks", "carbon", "furnace", "output", "gate"]


class MaterialMeasurementV1(BaseModel):
    """A measured quantity of material at a circuit stage (reconciliation domain, FP-09)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["material.measurement.v1"] = Field(
        default="material.measurement.v1", alias="schema"
    )
    measurement_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    stage: MaterialStage
    ts: AwareDatetime
    quantity: float = Field(ge=0)
    unit: str = Field(min_length=1)
    assay_ref: str | None = None
    quality: str | None = None
    source: str = Field(default="manual", min_length=1)
    provenance: Literal["measured"] = "measured"
