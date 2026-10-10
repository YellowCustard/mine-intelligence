"""The debounce / hysteresis state machine — where trust is won or lost.

GNSS jitter on a boundary must not generate a storm of enter/exit events
(brief §9). Entry is confirmed only after N consecutive fixes truly inside and,
where the zone sets ``min_entry_dwell_s``, that long since the first of them; exit
only after N consecutive fixes more than the hysteresis buffer *outside*. A fix
in the hysteresis band (outside the polygon but within the buffer) is neither —
it holds the current state, which is what damps the flap.

Fixes either side of a silence longer than ``max_gap_s`` (the platform's offline
threshold) are not consecutive: two lone fixes far apart in time corroborate
nothing, so the count starts again (audit M11).

The state object is a plain row (:class:`AssetZoneState`); this function mutates
it and returns the confirmed transition, if any.
"""

from __future__ import annotations

from datetime import UTC, datetime

from minemonitor.rules.config import RuleConfig
from minemonitor.storage.models import AssetZoneState


def step(
    state: AssetZoneState,
    *,
    inside_raw: bool,
    dist_outside_m: float,
    ts: datetime,
    cfg: RuleConfig,
    max_gap_s: float | None = None,
) -> str | None:
    """Advance the debounce machine one fix. Returns 'entry', 'exit', or None.

    ``state.last_ts`` must still be the previous fix's time (the caller updates it).
    While not inside, ``state.entered_at`` is when the current run of inside fixes
    began; once inside, it is when the asset went in.
    """
    last = state.last_ts
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)  # SQLite returns naive, Postgres aware
    if max_gap_s is not None and last is not None and (ts - last).total_seconds() > max_gap_s:
        state.consec_in = 0
        state.consec_out = 0

    if inside_raw:
        if not state.inside and (state.consec_in == 0 or state.entered_at is None):
            # A run that may become an entry starts here. (A row written before runs
            # were timed has a count but no start: time it from now.)
            state.entered_at = ts
        state.consec_in += 1
        state.consec_out = 0
    else:
        state.consec_in = 0
        if dist_outside_m > cfg.hysteresis_m:
            state.consec_out += 1
        # else: within the hysteresis band — hold; do not count as an exit.

    if not state.inside:
        since = state.entered_at
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        if (
            state.consec_in >= cfg.debounce_in
            and since is not None
            and (ts - since).total_seconds() >= cfg.min_entry_dwell_s
        ):
            state.inside = True
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
