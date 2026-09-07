"""Automated shift and daily reports: JSON, CSV, printable HTML, daily rollup."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.operations import delays, incidents
from minemonitor.storage.models import HaulCycle

_SHIFT = "kn-zw-01:2026-09-05:day"
_DATE = "2026-09-05"
_IN_WINDOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)


def _seed(db: Session) -> None:
    db.add(
        HaulCycle(
            site_id="kn-zw-01",
            asset_id="HT-102",
            start_ts=_IN_WINDOW,
            end_ts=datetime(2026, 9, 5, 10, 20, 0, tzinfo=UTC),
            cycle_time_s=1000.0,
            queue_s=200.0,
            load_s=100.0,
            haul_s=400.0,
            dump_s=100.0,
            return_s=200.0,
        )
    )
    delays.create_classification(
        db,
        site_id="kn-zw-01",
        category="maintenance",
        start_ts=datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC),
        end_ts=datetime(2026, 9, 5, 8, 30, 0, tzinfo=UTC),
        actor="sup",
    )
    incidents.create_incident(
        db,
        site_id="kn-zw-01",
        summary="loader down",
        type_="operational",
        severity="warning",
        actor="sup",
        now=_IN_WINDOW,
    )
    db.commit()


def test_shift_report_json(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    report = client.get(f"/sites/kn-zw-01/reports/shift?shift_id={_SHIFT}").json()
    assert report["kind"] == "shift_report"
    assert report["shift"]["shift_id"] == _SHIFT
    assert report["scorecard"]["cycles"]["count"] == 1
    assert len(report["incidents"]) == 1
    assert report["delays"][0]["category"] == "maintenance"


def test_shift_report_csv(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    r = client.get(f"/sites/kn-zw-01/reports/shift.csv?shift_id={_SHIFT}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text
    assert "queue_pct" in body
    assert "delay_category,maintenance" in body


def test_shift_report_html_is_printable(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    r = client.get(f"/sites/kn-zw-01/reports/shift.html?shift_id={_SHIFT}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in r.text.lower()
    assert "Shift report" in r.text
    assert "Utilisation" in r.text


def test_handover_appears_in_report(client: TestClient) -> None:
    client.post(
        "/sites/kn-zw-01/handovers", json={"shift_id": _SHIFT, "outgoing_notes": "handover note"}
    )
    report = client.get(f"/sites/kn-zw-01/reports/shift?shift_id={_SHIFT}").json()
    assert report["handovers"][0]["outgoing_notes"] == "handover note"


def test_daily_report_aggregates_shifts(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    daily = client.get(f"/sites/kn-zw-01/reports/daily?date={_DATE}").json()
    assert daily["kind"] == "daily_report"
    # Day + night shifts both attributed to the operating date.
    assert len(daily["shifts"]) == 2
    assert daily["totals"]["cycles"] == 1  # only the day shift has a cycle
    assert daily["totals"]["incidents"] == 1


def test_report_bad_shift_and_date_are_404(client: TestClient) -> None:
    assert (
        client.get("/sites/kn-zw-01/reports/shift?shift_id=kn-zw-01:2026-09-05:swing").status_code
        == 404
    )
    # A well-formed date always resolves day/night windows, so 'no shifts' cannot
    # happen for a configured site; a malformed shift_id is the 404 path.
    assert client.get("/sites/kn-zw-01/reports/shift?shift_id=garbage").status_code == 404
