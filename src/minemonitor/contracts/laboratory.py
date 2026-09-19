"""``laboratory.result.v1`` / ``laboratory.correction.v1`` — lab assay ingestion (FP-08).

Mine Monitor ingests laboratory instrument output **directly** (e.g. an Agilent 2000-series
AA spectrometer via SpectrAA) so a result is never retyped by hand. Two contracts keep the
audit backbone reconciliation (FP-09) depends on:

- ``laboratory.result.v1`` — the **measured** result. The value is stored write-once and the
  original raw record is hashed (``original_hash``), so tampering is detectable. Provenance is
  always ``measured``: it is an instrument reading, never an estimate.
- ``laboratory.correction.v1`` — an **append-only** correction that *references* a result
  without mutating it, carrying the corrected value, the actor and a reason. Full history is
  retained, so "what was measured, when, by which instrument, and who changed it" is always
  answerable.

``sample_ref`` is an opaque lab label, never personal data; the actor on a correction is a
user/operator reference and is audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LaboratoryResultV1(BaseModel):
    """A measured laboratory assay result (laboratory domain, FP-08)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["laboratory.result.v1"] = Field(default="laboratory.result.v1", alias="schema")
    result_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    sample_ref: str = Field(min_length=1)
    ts: datetime
    element: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    method: str | None = None
    batch_ref: str | None = None
    # Hex SHA-256 of the canonicalised original record — tamper-evidence for the immutable
    # original. The original itself is preserved separately (DB write-once column, plus a MinIO
    # object when offloaded).
    original_hash: str = Field(min_length=1)
    original_file_ref: str | None = None
    source: str = Field(default="spectraa", min_length=1)
    provenance: Literal["measured"] = "measured"


class LaboratoryCorrectionV1(BaseModel):
    """An append-only correction referencing a laboratory result (never a mutation)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["laboratory.correction.v1"] = Field(
        default="laboratory.correction.v1", alias="schema"
    )
    correction_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    corrected_value: float
    unit: str | None = None
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    ts: datetime
    provenance: Literal["corrected"] = "corrected"
