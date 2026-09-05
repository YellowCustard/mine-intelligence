"""``event.v1`` — anything worth a human's attention."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "zone_breach",
    "overspeed",
    "zone_dwell",
    "asset_offline",
    "geofence_exit",
    "proximity",
    "bog_precursor",
    "belt_state",
    "access_granted",
    "access_denied",
]
Severity = Literal["info", "warning", "critical"]
EventState = Literal["open", "acknowledged", "resolved"]


class EventV1(BaseModel):
    """A unified alarm-queue event.

    Every sensing modality emits this shape, so the control room groups by
    severity, not by source. ``advisory`` is always ``True``: the platform
    warns people, it never actuates plant.
    """

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["event.v1"] = Field(default="event.v1", alias="schema")
    event_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    ts: datetime
    type: EventType
    severity: Severity
    asset_id: str | None = None
    zone_id: str | None = None
    source: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    detail: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    advisory: Literal[True] = True
    state: EventState = "open"
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
