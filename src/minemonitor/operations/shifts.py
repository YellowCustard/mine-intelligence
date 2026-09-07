"""Shift resolution — the primary operational unit.

Shift *definitions* are per-site configuration (``ShiftDefinition``); shift
*instances* are derived on demand, never stored, so editing a definition changes
only how future windows resolve and never rewrites the immutable telemetry a past
window summarises. Windows are computed in the site's local timezone and may
cross midnight (brief §3); a stable ``shift_id`` keys analytics and reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import ShiftDefinition, Site

# Conventional two 12-hour shifts, used when a site has no definitions yet.
DEFAULT_DEFINITIONS: tuple[tuple[str, int, int], ...] = (("day", 6, 12), ("night", 18, 12))


@dataclass(frozen=True)
class ShiftWindow:
    """One resolved shift instance: a name and its ``[start, end)`` in UTC."""

    site_id: str
    name: str
    shift_id: str  # "<site>:<operating_date>:<name>" — stable across recomputes
    start: datetime
    end: datetime
    operating_date: date  # local calendar date the shift is attributed to (its start)

    def as_dict(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "shift_id": self.shift_id,
            "start": self.start,
            "end": self.end,
            "operating_date": self.operating_date.isoformat(),
        }


def _site_tz(session: Session, site_id: str) -> ZoneInfo:
    site = session.get(Site, site_id)
    return ZoneInfo(site.timezone if site and site.timezone else "Africa/Harare")


def definitions(session: Session, site_id: str) -> list[tuple[str, int, int]]:
    """Enabled ``(name, start_hour_local, duration_hours)`` for a site, or defaults."""
    rows = (
        session.execute(
            select(ShiftDefinition).where(
                ShiftDefinition.site_id == site_id, ShiftDefinition.enabled.is_(True)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        return [(r.name, r.start_hour_local, r.duration_hours) for r in rows]
    return list(DEFAULT_DEFINITIONS)


def _window(
    site_id: str, name: str, start_hour: int, duration_h: int, base_day: date, tz: ZoneInfo
) -> ShiftWindow:
    start_local = datetime(base_day.year, base_day.month, base_day.day, start_hour, tzinfo=tz)
    end_local = start_local + timedelta(hours=duration_h)
    return ShiftWindow(
        site_id=site_id,
        name=name,
        shift_id=f"{site_id}:{base_day.isoformat()}:{name}",
        start=start_local.astimezone(UTC),
        end=end_local.astimezone(UTC),
        operating_date=base_day,
    )


def resolve_shift_by_id(session: Session, site_id: str, shift_id: str) -> ShiftWindow | None:
    """Rebuild the window for a stable ``shift_id`` (``<site>:<date>:<name>``).

    Returns None if the id is malformed, names another site, or its date/name do
    not match a current definition — so a report or handover can only target a
    shift that actually exists for this site.
    """
    try:
        sid, date_str, name = shift_id.rsplit(":", 2)
        base_day = date.fromisoformat(date_str)
    except ValueError:
        return None
    if sid != site_id:
        return None
    tz = _site_tz(session, site_id)
    for def_name, start_hour, duration_h in definitions(session, site_id):
        if def_name == name:
            return _window(site_id, name, start_hour, duration_h, base_day, tz)
    return None


def resolve_shift(session: Session, site_id: str, at: datetime) -> ShiftWindow | None:
    """The shift instance containing ``at``, or None if it falls outside all shifts.

    Checks windows anchored to both the local date of ``at`` and the previous day,
    so a shift that crosses midnight is matched to the day it started on.
    """
    at = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    tz = _site_tz(session, site_id)
    defs = definitions(session, site_id)
    at_local_date = at.astimezone(tz).date()
    for base_day in (at_local_date, at_local_date - timedelta(days=1)):
        for name, start_hour, duration_h in defs:
            window = _window(site_id, name, start_hour, duration_h, base_day, tz)
            if window.start <= at < window.end:
                return window
    return None
