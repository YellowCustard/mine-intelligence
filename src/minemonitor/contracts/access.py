"""``access.event.v1`` — a gate/turnstile access decision (Mine Monitor Vision / FP-07).

Mine Monitor is the **record and intelligence** layer for site access; the gate hardware
and the vendor face/payroll system stay the identity authority and the enforcement point.
We ingest an access *decision event* only — never a biometric template or image (``CLAUDE.md``
§4): the ``credential_ref`` is an opaque tag/card/face-event id, and personal identity is a
foreign key to ``operators``, never a name in the payload.

``decision`` here is the **source system's** decision (what the gate did). Mine Monitor's own
authorisation rules (off-shift / suspended / non-inducted) run over this event and raise a
separate ``event.v1`` when they disagree — the intelligence on top of the record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccessDecision = Literal["granted", "denied"]


class AccessEventV1(BaseModel):
    """A single access decision at a gate/turnstile (access-control domain, FP-07)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["access.event.v1"] = Field(default="access.event.v1", alias="schema")
    event_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    ts: datetime
    source_system: str = Field(min_length=1)  # e.g. "alhua_gate", "ihua_tag", "turnstile"
    gate_id: str = Field(min_length=1)
    # An opaque credential reference (tag/card/face-event id) — NEVER a template or image.
    credential_ref: str | None = None
    operator_ref: str | None = None  # FK to operators; identity lives there, not here
    decision: AccessDecision  # the SOURCE system's decision
    reason: str | None = None
    search_selected: bool = False
    search_completed: bool | None = None
    metal_detected: bool | None = None
    advisory: Literal[True] = True
