"""The live cycle chart and the shift summary use the site's own shifts (audit M7).

The header already resolves the site's configured shift (``shift_definitions``), but the
live cycle summary and ``/shift-summary`` used a fixed 06:00 day / 18:00 night split. On a
site that runs three 8-hour shifts, the chart under "Shift B" counted cycles from a
12-hour "day" window and labelled them DAY.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.api.routers.stream import _cycle_summary
from minemonitor.storage.models import HaulCycle, ShiftDefinition

SITE = "kn-zw-01"
# Africa/Harare is UTC+2. Shifts A 06:00, B 14:00, C 22:00 local = 04:00, 12:00, 20:00 UTC.
_B_START = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
_C_START = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)


def _three_shifts(db: Session) -> None:
    for name, hour in (("A", 6), ("B", 14), ("C", 22)):
        db.add(
            ShiftDefinition(
                site_id=SITE, name=name, start_hour_local=hour, duration_hours=8, enabled=True
            )
        )
    db.commit()


def _cycle(db: Session, start: datetime, queue_s: float = 60.0) -> None:
    db.add(
        HaulCycle(
            site_id=SITE,
            asset_id="HT-102",
            start_ts=start,
            end_ts=start + timedelta(seconds=600),
            cycle_time_s=600.0,
            queue_s=queue_s,
            load_s=120.0,
            haul_s=200.0,
            dump_s=600.0 - queue_s - 120.0 - 200.0 - 80.0,
            return_s=80.0,
        )
    )
    db.commit()


def test_the_live_summary_counts_the_sites_current_shift(db_session: Session) -> None:
    _three_shifts(db_session)
    _cycle(db_session, _B_START - timedelta(hours=1), queue_s=300.0)  # shift A
    _cycle(db_session, _B_START + timedelta(hours=1))  # shift B
    _cycle(db_session, _B_START + timedelta(hours=2))  # shift B

    s = _cycle_summary(db_session, SITE, _B_START + timedelta(hours=3))

    assert s["shift"] == "B"
    assert s["shift_id"] == f"{SITE}:2026-09-05:B"
    assert s["cycles"] == 2
    assert s["queue_pct"] == 10.0  # 60 / 600: shift A's long queue is not in it


def test_an_empty_new_shift_shows_the_previous_one(db_session: Session) -> None:
    _three_shifts(db_session)
    _cycle(db_session, _C_START - timedelta(hours=1))  # late in shift B

    s = _cycle_summary(db_session, SITE, _C_START + timedelta(minutes=5))

    assert s["shift"] == "B"
    assert s["cycles"] == 1


def test_outside_every_shift_there_is_no_summary(db_session: Session) -> None:
    db_session.add(
        ShiftDefinition(
            site_id=SITE, name="morning", start_hour_local=8, duration_hours=4, enabled=True
        )
    )
    db_session.commit()
    _cycle(db_session, datetime(2026, 9, 5, 7, 0, tzinfo=UTC))  # 09:00 local, in "morning"

    s = _cycle_summary(db_session, SITE, datetime(2026, 9, 5, 13, 0, tzinfo=UTC))  # 15:00

    assert s["shift"] is None
    assert s["cycles"] == 0


def test_default_shifts_are_unchanged(db_session: Session) -> None:
    """A site with no definitions keeps day 06:00 / night 18:00."""
    _cycle(db_session, datetime(2026, 9, 5, 21, 0, tzinfo=UTC))  # 23:00 local, night

    s = _cycle_summary(db_session, SITE, datetime(2026, 9, 5, 23, 30, tzinfo=UTC))

    assert s["shift"] == "night"
    assert s["cycles"] == 1


def test_the_shift_summary_takes_the_sites_shift_names(
    client: TestClient, db_session: Session
) -> None:
    _three_shifts(db_session)
    _cycle(db_session, _B_START + timedelta(hours=1))

    r = client.get(f"/sites/{SITE}/shift-summary", params={"date": "2026-09-05", "shift": "B"})

    assert r.status_code == 200
    body = r.json()
    assert body["fleet"]["cycles"] == 1
    assert body["shift_id"] == f"{SITE}:2026-09-05:B"
    assert datetime.fromisoformat(body["start"]) == _B_START
    assert datetime.fromisoformat(body["end"]) == _B_START + timedelta(hours=8)


def test_the_shift_summary_refuses_a_shift_the_site_does_not_run(
    client: TestClient, db_session: Session
) -> None:
    _three_shifts(db_session)

    r = client.get(f"/sites/{SITE}/shift-summary", params={"date": "2026-09-05", "shift": "day"})

    assert r.status_code == 404
