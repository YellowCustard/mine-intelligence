"""Derived equipment state — observed facts vs inferred conditions.

A machine's state is derived from its latest fix, never stored as truth on the
telemetry. The critical distinction (brief data-quality intent) is between a
machine that *appears stopped* (a fresh fix, near-zero speed) and one whose
*data feed is unavailable* (no recent fix) — a comms outage must never be
counted as machine downtime. Each status carries whether it is directly
``observed`` or ``inferred``, so callers can label it honestly.

Zone context (loading/hauling/dumping) is layered on later; this module derives
the base motion/availability state from a single fix plus its age.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Base states derivable from one fix + its age.
MOVING = "moving"
IDLE = "idle"
STOPPED = "stopped"
OFFLINE = "offline"
UNKNOWN = "unknown"

# Below this speed (kph) a fix counts as stationary — matches the cycle engine.
MOVE_THRESHOLD_KPH = 3.0

OBSERVED = "observed"
INFERRED = "inferred"


@dataclass(frozen=True)
class EquipmentStatus:
    state: str
    basis: str  # OBSERVED (measured from a fresh fix) | INFERRED (deduced from absence)
    data_age_s: float | None  # seconds since the last fix, or None if never seen
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "basis": self.basis,
            "data_age_s": self.data_age_s,
            "reason": self.reason,
        }


def derive_state(
    *,
    latest_ts: datetime | None,
    speed_kph: float | None,
    ignition: bool | None,
    now: datetime,
    offline_after_s: float,
    move_threshold_kph: float = MOVE_THRESHOLD_KPH,
) -> EquipmentStatus:
    """Derive a machine's state from its most recent fix and how old it is."""
    if latest_ts is None:
        return EquipmentStatus(UNKNOWN, INFERRED, None, "no telemetry on record")

    latest = latest_ts if latest_ts.tzinfo is not None else latest_ts.replace(tzinfo=UTC)
    age = (now - latest).total_seconds()

    if age > offline_after_s:
        # No fix recently: the tracker or the link is down. This is NOT the same
        # as the machine being stopped — do not count it as machine downtime.
        return EquipmentStatus(
            OFFLINE, INFERRED, age, f"no fix for {int(age)}s — data feed unavailable"
        )

    # A fresh fix: the motion state is directly observed.
    if speed_kph is not None and speed_kph >= move_threshold_kph:
        return EquipmentStatus(MOVING, OBSERVED, age, f"moving at {speed_kph:.0f} kph")
    if ignition is False:
        return EquipmentStatus(STOPPED, OBSERVED, age, "stationary, ignition off")
    return EquipmentStatus(IDLE, OBSERVED, age, "stationary, ignition on/unknown")


def state_now(
    *,
    latest_ts: datetime | None,
    speed_kph: float | None,
    ignition: bool | None,
    offline_after_s: float,
) -> EquipmentStatus:
    """Convenience: derive state as of ``datetime.now(UTC)``."""
    return derive_state(
        latest_ts=latest_ts,
        speed_kph=speed_kph,
        ignition=ignition,
        now=datetime.now(UTC),
        offline_after_s=offline_after_s,
    )
