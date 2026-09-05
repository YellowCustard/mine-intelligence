"""The debounce / hysteresis state machine — where trust is won or lost.

GNSS jitter on a boundary must not generate a storm of enter/exit events
(brief §9). Entry is confirmed only after N consecutive fixes truly inside; exit
only after N consecutive fixes more than the hysteresis buffer *outside*. A fix
in the hysteresis band (outside the polygon but within the buffer) is neither —
it holds the current state, which is what damps the flap.

The state object is a plain row (:class:`AssetZoneState`); this function mutates
it and returns the confirmed transition, if any.
"""

from __future__ import annotations

from datetime import datetime

from minemonitor.rules.config import RuleConfig
from minemonitor.storage.models import AssetZoneState


def step(
    state: AssetZoneState,
    *,
    inside_raw: bool,
    dist_outside_m: float,
    ts: datetime,
    cfg: RuleConfig,
) -> str | None:
    """Advance the debounce machine one fix. Returns 'entry', 'exit', or None."""
    if inside_raw:
        state.consec_in += 1
        state.consec_out = 0
    else:
        state.consec_in = 0
        if dist_outside_m > cfg.hysteresis_m:
            state.consec_out += 1
        # else: within the hysteresis band — hold; do not count as an exit.

    if not state.inside:
        if state.consec_in >= cfg.debounce_in:
            state.inside = True
            state.entered_at = ts
            state.consec_out = 0
            return "entry"
        return None

    # Currently confirmed inside.
    if state.consec_out >= cfg.exit_debounce:
        state.inside = False
        state.entered_at = None
        state.consec_in = 0
        # A new visit starts fresh: reset per-episode rule state.
        state.overspeed_consec = 0
        state.overspeed_fired = False
        state.stationary_since = None
        state.dwell_fired = False
        return "exit"
    return None
