"""Shift windows. Store UTC, reason in local time; shifts cross midnight (brief §3).

A day boundary is never assumed to equal a shift boundary. Defaults: two 12-hour
shifts, day starting 06:00 local.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

DAY = "day"
NIGHT = "night"


def shift_bounds(
    local_date: date,
    shift: str,
    tz_name: str = "Africa/Harare",
    *,
    day_start_hour: int = 6,
    shift_hours: int = 12,
) -> tuple[datetime, datetime]:
    """Return the [start, end) of a shift as timezone-aware UTC datetimes."""
    tz = ZoneInfo(tz_name)
    day_start = datetime(
        local_date.year, local_date.month, local_date.day, day_start_hour, tzinfo=tz
    )
    if shift == DAY:
        start_local = day_start
    elif shift == NIGHT:
        start_local = day_start + timedelta(hours=shift_hours)  # crosses midnight
    else:
        raise ValueError(f"unknown shift {shift!r}")
    end_local = start_local + timedelta(hours=shift_hours)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
