"""HTTP coverage for the alarm-queue list endpoint and its filters (Phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.storage.models import Event

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _event(db: Session, eid: str, *, severity: str, state: str, mins_ago: int) -> None:
    db.add(
        Event(
            event_id=eid,
            site_id="kn-zw-01",
            ts=_NOW - timedelta(minutes=mins_ago),
            type="zone_breach",
            severity=severity,
            source="gnss_geofence",
            summary=eid,
            advisory=True,
            state=state,
        )
    )


def test_list_newest_first_and_filters(client: TestClient, db_session: Session) -> None:
    _event(db_session, "old", severity="warning", state="open", mins_ago=30)
    _event(db_session, "new", severity="critical", state="open", mins_ago=1)
    _event(db_session, "done", severity="critical", state="resolved", mins_ago=5)
    db_session.commit()

    all_events = client.get("/sites/kn-zw-01/events").json()
    assert [e["event_id"] for e in all_events] == ["new", "done", "old"]  # newest first

    crit = client.get("/sites/kn-zw-01/events", params={"severity": "critical"}).json()
    assert {e["event_id"] for e in crit} == {"new", "done"}

    open_only = client.get("/sites/kn-zw-01/events", params={"state": "open"}).json()
    assert {e["event_id"] for e in open_only} == {"new", "old"}


def test_list_is_site_scoped(client: TestClient, db_session: Session) -> None:
    _event(db_session, "here", severity="warning", state="open", mins_ago=1)
    db_session.commit()
    # A different site sees none of kn-zw-01's events.
    from minemonitor.storage.models import Site

    db_session.add(Site(site_id="other", name="Other", timezone="Africa/Harare"))
    db_session.commit()
    assert client.get("/sites/other/events").json() == []
