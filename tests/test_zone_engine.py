"""Unit tests for the debounce/hysteresis state machine — anti-flap correctness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minemonitor.rules.config import RuleConfig
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
