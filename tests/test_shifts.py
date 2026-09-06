"""Shift resolution: defaults, cross-midnight, custom definitions, and gaps."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from minemonitor.operations.shifts import resolve_shift
from minemonitor.storage.models import ShiftDefinition

# Africa/Harare is UTC+2 year-round (no DST). db_session seeds site kn-zw-01 there
# with no shift definitions, so the default day(06:00,12h)/night(18:00,12h) apply.


def test_default_day_shift(db_session: Session) -> None:
    at = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)  # 12:00 local
    w = resolve_shift(db_session, "kn-zw-01", at)
    assert w is not None
    assert w.name == "day"
    assert w.operating_date.isoformat() == "2026-09-05"
    assert w.shift_id == "kn-zw-01:2026-09-05:day"
    assert w.start <= at < w.end


def test_default_night_shift_crosses_midnight(db_session: Session) -> None:
    at = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)  # 22:00 local
    w = resolve_shift(db_session, "kn-zw-01", at)
    assert w is not None and w.name == "night"
    assert w.operating_date.isoformat() == "2026-09-05"  # attributed to its start day


def test_early_morning_belongs_to_previous_night(db_session: Session) -> None:
    at = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)  # 05:00 local — before the day shift
    w = resolve_shift(db_session, "kn-zw-01", at)
    assert w is not None and w.name == "night"
    assert w.operating_date.isoformat() == "2026-09-04"  # the prior day's night


def test_custom_definition_and_gap_returns_none(db_session: Session) -> None:
    # A single short shift replaces the defaults; times outside it resolve to None.
    db_session.add(
        ShiftDefinition(
            site_id="kn-zw-01", name="morning", start_hour_local=8, duration_hours=4, enabled=True
        )
    )
    db_session.commit()
    inside = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)  # 09:00 local — within 08–12
    outside = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)  # 14:00 local — no shift defined
    assert resolve_shift(db_session, "kn-zw-01", inside).name == "morning"
    assert resolve_shift(db_session, "kn-zw-01", outside) is None


def test_disabled_definition_is_ignored(db_session: Session) -> None:
    # Day enabled, night disabled: midday resolves to day; night-time resolves to
    # nothing (the disabled night is not used, and an enabled row exists so the
    # default set is not resurrected).
    db_session.add(
        ShiftDefinition(
            site_id="kn-zw-01", name="day", start_hour_local=6, duration_hours=12, enabled=True
        )
    )
    db_session.add(
        ShiftDefinition(
            site_id="kn-zw-01", name="night", start_hour_local=18, duration_hours=12, enabled=False
        )
    )
    db_session.commit()
    midday = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)  # 12:00 local
    night = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)  # 22:00 local
    assert resolve_shift(db_session, "kn-zw-01", midday).name == "day"
    assert resolve_shift(db_session, "kn-zw-01", night) is None
