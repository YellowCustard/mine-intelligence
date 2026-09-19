"""Vision contracts. Currently: ``vision.vendor_event.v1`` (NVR event ingestion, Stage A).

The near-term vision path (``docs/VISION_BUILD_GATE.md`` §2/§4.5/§8) ingests the AI events the
Dahua NVR already produces. It is **vendor event ingestion, not first-party perception**: the
provenance is a distinct ``vendor_inferred`` class, the vendor's confidence is carried verbatim
and never upgraded to a first-party confidence, and **no identity/biometric field is ever
admitted** (``CLAUDE.md`` §4). First-party ``vision.observation.v1`` and the rest of the
build-gate contracts land later, behind the §14 review gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VisionVendorEventV1(BaseModel):
    """A normalised NVR AI event (vision domain, Stage A). Vendor-inferred provenance."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["vision.vendor_event.v1"] = Field(
        default="vision.vendor_event.v1", alias="schema"
    )
    vendor_event_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    camera_id: str | None = None
    channel: str | None = None
    vendor_type: str = Field(min_length=1)
    normalized_type: str = Field(min_length=1)
    # The vendor's own confidence — carried, NEVER upgraded to a first-party confidence.
    vendor_confidence: float | None = Field(default=None, ge=0, le=1)
    vendor_rule_name: str | None = None
    ts: datetime
    clip_ref: str | None = None
    provenance: Literal["vendor_inferred"] = "vendor_inferred"
    advisory: Literal[True] = True
