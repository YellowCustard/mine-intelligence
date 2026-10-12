"""A missed search is escalated once, recorded on the passage itself (soak).

The tick found missed searches by reading every selected search never completed, ever, and
asking the events table whether each had been raised yet: a query per missed search, every
tick, growing for as long as the gate runs. And when events retention purged an old alarm,
the passage it was raised for (access events are kept) looked new again, and was raised
again a year after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, event
from sqlalchemy.orm import Session

from minemonitor.access import service
from minemonitor.storage.models import AccessEvent, Event, Operator

SITE = "kn-zw-01"
_TS = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
_LATE = _TS + timedelta(hours=1)  # past the 15-minute grace


def _passages(db: Session, n: int) -> list[AccessEvent]:
    """``n`` authorised passages, each selected for search (rate 100), none searched."""
    db.add(
        Operator(
            operator_id="OP-001",
            site_id=SITE,
            display_name="Test Operator",
            suspended=False,
            inducted=True,
            created_at=_TS,
        )
    )
    rows = []
    for i in range(n):
        row, _created, _alarm = service.ingest_access_event(
            db,
            SITE,
            source_system="g",
            source_event_id=f"p-{i}",
            gate_id="main-gate",
            decision="granted",
            ts=_TS,
            now=_TS,
            operator_ref="OP-001",
            search_rate_percent=100,
        )
        rows.append(row)
    db.commit()
    return rows


def _missed(db: Session, now: datetime) -> list:
    return service.detect_missed_searches(db, SITE, now=now, grace_s=900)


def test_a_missed_search_is_not_raised_again_when_retention_purges_its_alarm(
    db_session: Session,
) -> None:
    _passages(db_session, 1)
    (alarm,) = _missed(db_session, _LATE)
    # A year on, events retention deletes the alarm. Access events have no retention class,
    # so the passage is still there.
    db_session.execute(delete(Event).where(Event.event_id == alarm.event_id))
    db_session.commit()
    assert _missed(db_session, _LATE + timedelta(days=366)) == []


def test_the_tick_reads_no_passage_it_has_already_escalated(db_session: Session) -> None:
    _passages(db_session, 20)
    assert len(_missed(db_session, _LATE)) == 20
    seen: list[str] = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert _missed(db_session, _LATE + timedelta(minutes=1)) == []
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(seen) == 1  # one query, which finds nothing to escalate


def test_a_passage_escalated_before_the_marker_existed_is_not_raised_again(
    db_session: Session,
) -> None:
    (passage,) = _passages(db_session, 1)
    (alarm,) = _missed(db_session, _LATE)
    # As an older version left it: the alarm raised, the passage not marked.
    passage.search_escalated_at = None
    db_session.commit()
    assert _missed(db_session, _LATE + timedelta(minutes=1)) == []
    assert passage.search_escalated_at is not None
    assert db_session.get(Event, alarm.event_id) is not None


def test_a_search_completed_after_its_alarm_stays_escalated_once(db_session: Session) -> None:
    (passage,) = _passages(db_session, 1)
    assert len(_missed(db_session, _LATE)) == 1
    service.complete_search(db_session, SITE, passage.id, now=_LATE)
    db_session.commit()
    assert _missed(db_session, _LATE + timedelta(minutes=1)) == []
