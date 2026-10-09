"""``generator.reading.v1`` — a measured DSE genset controller reading (FP-11).

A DSE controller already measures engine hours, running state, fuel level and faults, so this
is real measured data, not a fabricated sensor. The reading feeds the existing maintenance
run-hours health (``maintenance.service.assess_health``) and raises advisory ``event.v1``
alarms on a fault or an unexpected run. **Advisory only** — the platform monitors and alerts;
it never controls a genset. Nothing here is fabricated: a value the controller does not report
is ``null``, never invented.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class GeneratorReadingV1(BaseModel):
    """A measured periodic generator reading (generator domain, FP-11)."""

    model_config = ConfigDict(extra="forbid")

    schema_: Literal["generator.reading.v1"] = Field(default="generator.reading.v1", alias="schema")
    reading_id: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source: str = Field(default="dse_sim", min_length=1)
    ts: AwareDatetime
    engine_hours: float | None = Field(default=None, ge=0)
    running: bool
    fuel_level_pct: float | None = Field(default=None, ge=0, le=100)
    # The schedule/controller intent; when False and ``running`` is True → unexpected-run alarm.
    expected_running: bool | None = None
    fault: bool = False
    fault_code: str | None = None
    measured: Literal[True] = True
