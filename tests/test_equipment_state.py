"""Derived equipment state, and the observed-vs-inferred / stopped-vs-offline split."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minemonitor.operations import equipment
from minemonitor.operations.equipment import INFERRED, OBSERVED, derive_state

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _fresh(seconds_ago: float) -> datetime:
    return _NOW - timedelta(seconds=seconds_ago)


def test_moving_is_observed() -> None:
    s = derive_state(
        latest_ts=_fresh(5), speed_kph=40.0, ignition=True, now=_NOW, offline_after_s=600
    )
    assert s.state == equipment.MOVING and s.basis == OBSERVED


def test_idle_when_stationary_ignition_on() -> None:
    s = derive_state(
        latest_ts=_fresh(5), speed_kph=0.0, ignition=True, now=_NOW, offline_after_s=600
    )
    assert s.state == equipment.IDLE and s.basis == OBSERVED


def test_stopped_when_stationary_ignition_off() -> None:
    s = derive_state(
        latest_ts=_fresh(5), speed_kph=0.0, ignition=False, now=_NOW, offline_after_s=600
    )
    assert s.state == equipment.STOPPED and s.basis == OBSERVED


def test_offline_is_inferred_and_distinct_from_stopped() -> None:
    # A fix 20 minutes old with the threshold at 10 minutes: the data feed is down.
    s = derive_state(
        latest_ts=_fresh(1200), speed_kph=0.0, ignition=False, now=_NOW, offline_after_s=600
    )
    assert s.state == equipment.OFFLINE
    assert s.basis == INFERRED  # never counted as observed machine downtime
    assert s.data_age_s is not None and s.data_age_s > 600
    assert "data feed" in s.reason


def test_unknown_when_never_seen() -> None:
    s = derive_state(latest_ts=None, speed_kph=None, ignition=None, now=_NOW, offline_after_s=600)
    assert s.state == equipment.UNKNOWN and s.basis == INFERRED and s.data_age_s is None


def test_naive_timestamp_is_normalised() -> None:
    naive = _NOW.replace(tzinfo=None) - timedelta(seconds=10)
    s = derive_state(latest_ts=naive, speed_kph=50.0, ignition=True, now=_NOW, offline_after_s=600)
    assert s.state == equipment.MOVING  # no tz-comparison crash
