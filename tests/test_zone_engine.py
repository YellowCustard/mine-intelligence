"""Unit tests for the debounce/hysteresis state machine — anti-flap correctness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minemonitor.rules.config import RuleConfig, parse_rules
from minemonitor.storage.models import AssetZoneState
from minemonitor.zones import engine

CFG = RuleConfig()  # debounce_in=2, exit_debounce=2, hysteresis_m=15


def _state() -> AssetZoneState:
    return AssetZoneState(
        site_id="s",
        asset_id="a",
        zone_id="z",
        inside=False,
        consec_in=0,
        consec_out=0,
        overspeed_consec=0,
        overspeed_fired=False,
        dwell_fired=False,
    )


def _ts(i: int) -> datetime:
    return datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC) + timedelta(seconds=i)


def _step(state, inside, dist, i):
    return engine.step(state, inside_raw=inside, dist_outside_m=dist, ts=_ts(i), cfg=CFG)


def test_entry_needs_two_consecutive_inside() -> None:
    st = _state()
    assert _step(st, True, 0, 0) is None  # 1st inside — not yet
    assert _step(st, True, 0, 1) == "entry"  # 2nd consecutive — confirmed
    assert st.inside is True


def test_single_inside_fix_does_not_enter() -> None:
    st = _state()
    assert _step(st, True, 0, 0) is None
    assert _step(st, False, 20, 1) is None  # one in, then clearly out
    assert st.inside is False


def test_exit_needs_two_consecutive_beyond_buffer() -> None:
    st = _state()
    _step(st, True, 0, 0)
    _step(st, True, 0, 1)  # entered
    assert _step(st, False, 20, 2) is None  # 1st beyond buffer
    assert _step(st, False, 20, 3) == "exit"  # 2nd — confirmed exit
    assert st.inside is False


def test_within_hysteresis_band_does_not_exit() -> None:
    """A fix outside the polygon but within the buffer holds membership."""
    st = _state()
    _step(st, True, 0, 0)
    _step(st, True, 0, 1)  # entered
    for i in range(2, 20):
        # Oscillate just outside (within 15 m buffer) and back inside.
        _step(st, False, 8, i)
        _step(st, True, 0, i)
    assert st.inside is True  # never flapped out


def test_boundary_hugging_never_enters() -> None:
    """Alternating inside / just-outside never reaches 2 consecutive inside."""
    st = _state()
    entries = 0
    for i in range(40):
        t = _step(st, i % 2 == 0, 5, i)  # inside on evens, in-buffer on odds
        if t == "entry":
            entries += 1
    assert entries == 0
    assert st.inside is False


# --- Time, not just fix count (audit M11) -------------------------------------------


def _feed(state, inside, dist, i, cfg=CFG, max_gap_s=None):
    """One fix through the engine, then the watermark the pipeline keeps."""
    t = engine.step(
        state, inside_raw=inside, dist_outside_m=dist, ts=_ts(i), cfg=cfg, max_gap_s=max_gap_s
    )
    state.last_ts = _ts(i)
    return t


def test_a_zone_can_require_time_inside_before_entry() -> None:
    cfg = RuleConfig(min_entry_dwell_s=5)
    st = _state()
    seen = [_feed(st, True, 0, i, cfg) for i in range(7)]
    # Two fixes are not enough on their own: entry waits until 5 s after the first.
    assert seen == [None, None, None, None, None, "entry", None]
    assert st.entered_at == _ts(0)  # when it went in, not when that was confirmed


def test_leaving_restarts_the_entry_clock() -> None:
    cfg = RuleConfig(min_entry_dwell_s=5)
    st = _state()
    seen = [_feed(st, True, 0, i, cfg) for i in range(3)]
    seen.append(_feed(st, False, 40, 3, cfg))  # out before 5 s
    seen += [_feed(st, True, 0, i, cfg) for i in range(4, 10)]
    assert seen.index("entry") == 9  # 5 s after the second run began at 4 s
    assert st.entered_at == _ts(4)


def test_min_entry_dwell_is_zone_data() -> None:
    assert parse_rules("restricted", {"min_entry_dwell_s": 4}).min_entry_dwell_s == 4.0
    assert parse_rules("restricted", {}).min_entry_dwell_s == 0.0  # the fix count alone


def test_fixes_either_side_of_an_outage_are_not_consecutive() -> None:
    st = _state()
    assert _feed(st, True, 0, 0, max_gap_s=600) is None
    # Silent for 10 min 1 s: two lone fixes that far apart do not confirm anything.
    assert _feed(st, True, 0, 601, max_gap_s=600) is None
    assert _feed(st, True, 0, 602, max_gap_s=600) == "entry"


def test_an_outage_does_not_count_toward_an_exit() -> None:
    st = _state()
    _feed(st, True, 0, 0, max_gap_s=600)
    _feed(st, True, 0, 1, max_gap_s=600)  # entered
    assert _feed(st, False, 40, 2, max_gap_s=600) is None
    assert _feed(st, False, 40, 900, max_gap_s=600) is None  # back after an outage
    assert _feed(st, False, 40, 901, max_gap_s=600) == "exit"


def test_a_short_gap_keeps_the_run() -> None:
    st = _state()
    assert _feed(st, True, 0, 0, max_gap_s=600) is None
    assert _feed(st, True, 0, 300, max_gap_s=600) == "entry"  # a slow tracker, not an outage


def test_a_run_counted_before_runs_were_timed_still_enters() -> None:
    st = _state()
    st.consec_in = 1  # one inside fix counted by the previous release, with no start time
    assert st.entered_at is None
    assert _feed(st, True, 0, 1) == "entry"
