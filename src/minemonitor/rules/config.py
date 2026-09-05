"""Parse a zone's ``rules`` JSONB payload into a typed config with defaults.

Adding or changing a zone rule is a data change (edit the zone's ``rules``), never
a deploy (brief §9). Unknown keys are ignored so the payload can grow.
"""

from __future__ import annotations

from dataclasses import dataclass

# Debounce / hysteresis defaults (brief §9). Configurable per zone.
DEFAULT_DEBOUNCE_IN = 2
DEFAULT_EXIT_DEBOUNCE = 2
DEFAULT_HYSTERESIS_M = 15.0
DEFAULT_OVERSPEED_CONSECUTIVE = 3
# Below this speed an asset counts as stationary (for dwell).
STATIONARY_KPH = 1.5


@dataclass(frozen=True)
class RuleConfig:
    """Effective rule parameters for one zone."""

    debounce_in: int = DEFAULT_DEBOUNCE_IN
    exit_debounce: int = DEFAULT_EXIT_DEBOUNCE
    hysteresis_m: float = DEFAULT_HYSTERESIS_M
    # restricted
    authorized_classes: tuple[str, ...] | None = None
    # speed_limited
    speed_limit_kph: float | None = None
    overspeed_consecutive: int = DEFAULT_OVERSPEED_CONSECUTIVE
    # dwell (any zone)
    dwell_s: float | None = None
    # severity overrides
    breach_severity: str = "critical"
    overspeed_severity: str = "warning"
    dwell_severity: str = "warning"


def parse_rules(kind: str, rules: dict | None) -> RuleConfig:
    """Build a :class:`RuleConfig` from a zone's kind and rules payload."""
    r = rules or {}
    authorized = r.get("authorized_classes")
    return RuleConfig(
        debounce_in=int(r.get("debounce_in", DEFAULT_DEBOUNCE_IN)),
        exit_debounce=int(r.get("exit_debounce", DEFAULT_EXIT_DEBOUNCE)),
        hysteresis_m=float(r.get("hysteresis_m", DEFAULT_HYSTERESIS_M)),
        authorized_classes=tuple(authorized) if authorized is not None else None,
        speed_limit_kph=(
            float(r["speed_limit_kph"]) if r.get("speed_limit_kph") is not None else None
        ),
        overspeed_consecutive=int(r.get("overspeed_consecutive", DEFAULT_OVERSPEED_CONSECUTIVE)),
        dwell_s=float(r["dwell_s"]) if r.get("dwell_s") is not None else None,
        breach_severity=str(r.get("severity", "critical")),
        overspeed_severity=str(r.get("severity", "warning")),
        dwell_severity=str(r.get("severity", "warning")),
    )
