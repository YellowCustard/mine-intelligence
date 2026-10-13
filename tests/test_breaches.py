"""Personal-data breach register and notice deadlines (CDPA / SI 155 of 2024, plan C-1).

A breach the mine (as controller) records here gets two deadlines, fixed when it is
recorded: notice to the regulator (reported: 24 h) and to the people affected (reported:
72 h). The maintenance tick raises one advisory warning as each deadline approaches and one
critical alarm if it passes unsent. Recording the notice, or closing the breach, stops them.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.breaches.service import check_breach_deadlines, record_breach
from minemonitor.config import get_settings
from minemonitor.retention import run_retention
from minemonitor.storage.models import (
    AuditLog,
    BreachOperator,
    BreachRecord,
    Event,
    Operator,
    Site,
)
from tests.conftest import SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
T0 = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)  # 08:00 in Harare
SUMMARY = "Laptop with an operator export left in a taxi"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(db: Session, operator_id: str, name: str | None = "Alice Driver") -> str:
    db.add(
        Operator(
            operator_id=operator_id,
            site_id=SITE,
            display_name=name,
            employee_ref="E-1",
            contact="alice@example.org",
            created_at=T0,
        )
    )
    db.commit()
    return operator_id


def _record(db: Session, **kw: object) -> BreachRecord:
    args: dict = {
        "site_id": SITE,
        "discovered_at": T0,
        "occurred_at": None,
        "summary": SUMMARY,
        "data_classes": ["operator_records"],
        "operator_ids": [],
        "notify_individuals": True,
        "actor": "admin",
        "now": T0,
    }
    args.update(kw)
    row = record_breach(db, **args)  # type: ignore[arg-type]
    db.commit()
    return row


def _alarms(db: Session) -> list[Event]:
    return list(
        db.execute(select(Event).where(Event.type == "breach_notice_due").order_by(Event.ts))
        .scalars()
        .all()
    )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- Deadlines ---------------------------------------------------------------------------


def test_recording_a_breach_fixes_both_deadlines(client: TestClient, db_session: Session) -> None:
    op = _operator(db_session, "op-1")
    discovered = datetime.now(UTC) - timedelta(hours=1)
    r = client.post(
        f"/sites/{SITE}/breaches",
        json={
            "discovered_at": discovered.isoformat(),
            "summary": SUMMARY,
            "data_classes": ["operator_records", "access_events"],
            "operator_ids": [op],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert datetime.fromisoformat(body["regulator"]["due_at"]) == discovered + timedelta(hours=24)
    assert datetime.fromisoformat(body["individuals"]["due_at"]) == discovered + timedelta(hours=72)
    assert body["regulator"]["status"] == "pending"
    assert body["operator_ids"] == [op]
    log = db_session.execute(
        select(AuditLog).where(AuditLog.action == "breach.record")
    ).scalar_one()
    assert log.entity_id == body["breach_id"] and log.site_id == SITE
    assert SUMMARY not in str(log.detail)


def test_deadlines_are_settings(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_BREACH_REGULATOR_NOTICE_H", "48")
    monkeypatch.setenv("MM_BREACH_INDIVIDUALS_NOTICE_H", "96")
    monkeypatch.setenv("MM_BREACH_DEADLINE_FROM", "occurred")
    get_settings.cache_clear()
    occurred = T0 - timedelta(hours=10)
    row = _record(db_session, occurred_at=occurred)
    assert _aware(row.regulator_due_at) == occurred + timedelta(hours=48)
    assert row.individuals_due_at is not None
    assert _aware(row.individuals_due_at) == occurred + timedelta(hours=96)
    # With the clock counted from occurrence but no occurrence recorded, discovery is used.
    row = _record(db_session)
    assert _aware(row.regulator_due_at) == T0 + timedelta(hours=48)


def test_a_settings_change_does_not_move_a_recorded_deadline(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _record(db_session)
    monkeypatch.setenv("MM_BREACH_REGULATOR_NOTICE_H", "2")
    get_settings.cache_clear()
    # Under the new setting the notice would be overdue; under the recorded deadline it is not.
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=3)) == []
    db_session.refresh(row)
    assert _aware(row.regulator_due_at) == T0 + timedelta(hours=24)


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"discovered_at": "2999-01-01T00:00:00Z"}, "future"),
        (
            {"discovered_at": "2026-09-05T06:00:00Z", "occurred_at": "2026-09-05T07:00:00Z"},
            "occurred_at",
        ),
        ({"discovered_at": "2026-09-05T06:00:00Z", "operator_ids": ["nobody"]}, "nobody"),
        ({"discovered_at": "2026-09-05T06:00:00Z", "data_classes": []}, "data_classes"),
        ({"discovered_at": "2026-09-05T06:00:00Z", "data_classes": ["faces"]}, "data_classes"),
    ],
)
def test_an_impossible_breach_record_is_refused(
    client: TestClient, db_session: Session, body: dict, fragment: str
) -> None:
    payload = {"summary": SUMMARY, "data_classes": ["operator_records"], **body}
    r = client.post(f"/sites/{SITE}/breaches", json=payload)
    assert r.status_code == 422, r.text
    assert fragment in r.text
    assert db_session.execute(select(BreachRecord)).first() is None


def test_an_operator_of_another_site_cannot_be_named(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(Site(site_id="other-site", name="Other", timezone="Africa/Harare"))
    db_session.flush()  # Operator sorts before Site: write the site first
    db_session.add(
        Operator(operator_id="op-x", site_id="other-site", display_name="X", created_at=T0)
    )
    db_session.commit()
    r = client.post(
        f"/sites/{SITE}/breaches",
        json={
            "discovered_at": "2026-09-05T06:00:00Z",
            "summary": SUMMARY,
            "data_classes": ["operator_records"],
            "operator_ids": ["op-x"],
        },
    )
    assert r.status_code == 422


# --- Alarms ------------------------------------------------------------------------------


def test_each_deadline_warns_once_then_alarms_once_when_missed(db_session: Session) -> None:
    _record(db_session)
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=17)) == []

    warned = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=18))
    assert [(e.severity, e.detail["notice"]) for e in warned] == [("warning", "regulator")]
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=20)) == []

    missed = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=24))
    assert [(e.severity, e.detail["notice"]) for e in missed] == [("critical", "regulator")]
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=30)) == []

    later = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=73))
    # The individuals' warning window was never seen by a tick; only the overdue alarm fires.
    assert [(e.severity, e.detail["notice"]) for e in later] == [("critical", "individuals")]
    assert len(_alarms(db_session)) == 3
    assert all(a.advisory for a in _alarms(db_session))


def test_an_alarm_names_the_deadline_but_not_the_breach_or_the_people(
    db_session: Session,
) -> None:
    op = _operator(db_session, "op-1")
    row = _record(db_session, operator_ids=[op])
    (ev,) = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=24))
    # Alarms are pushed to webhook, email and WhatsApp; the summary and the people stay here.
    text = f"{ev.summary} {ev.detail} {ev.evidence}"
    assert SUMMARY not in text and op not in text and "Alice" not in text
    assert row.breach_id in text
    assert "POTRAZ" in ev.summary
    assert "08:00" in ev.summary  # the deadline in site time (Africa/Harare)


def test_recording_a_notice_stops_its_alarms(client: TestClient, db_session: Session) -> None:
    discovered = datetime.now(UTC) - timedelta(hours=20)
    r = client.post(
        f"/sites/{SITE}/breaches",
        json={
            "discovered_at": discovered.isoformat(),
            "summary": SUMMARY,
            "data_classes": ["positions"],
        },
    )
    breach_id = r.json()["breach_id"]
    (warning,) = check_breach_deadlines(db_session, SITE)
    assert warning.detail["notice"] == "regulator"

    r = client.post(
        f"/sites/{SITE}/breaches/{breach_id}/notices/regulator",
        json={"notified_at": datetime.now(UTC).isoformat(), "reference": "POTRAZ/DP/2026/017"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["regulator"]["status"] == "sent"
    assert r.json()["regulator"]["reference"] == "POTRAZ/DP/2026/017"
    ev = db_session.get(Event, warning.event_id)
    db_session.refresh(ev)
    assert ev.state == "acknowledged" and ev.acknowledged_by == "admin"
    # Past the regulator deadline: no overdue alarm, because the notice was sent.
    later = check_breach_deadlines(db_session, SITE, now=discovered + timedelta(hours=25))
    assert later == []


def test_notice_to_individuals_can_be_recorded_as_not_required(
    client: TestClient, db_session: Session
) -> None:
    row = _record(db_session, notify_individuals=False)
    assert row.individuals_due_at is None
    events = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=100))
    assert [e.detail["notice"] for e in events] == ["regulator"]
    r = client.post(
        f"/sites/{SITE}/breaches/{row.breach_id}/notices/individuals",
        json={"notified_at": "2026-09-06T06:00:00Z"},
    )
    assert r.status_code == 409
    assert (
        client.get(f"/sites/{SITE}/breaches/{row.breach_id}").json()["individuals"]["status"]
        == "not_required"
    )


def test_closing_a_breach_stops_its_alarms_and_records_what_was_outstanding(
    client: TestClient, db_session: Session
) -> None:
    row = _record(db_session)
    (warning,) = check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=19))
    r = client.post(
        f"/sites/{SITE}/breaches/{row.breach_id}/close", json={"note": "Not personal data."}
    )
    assert r.status_code == 200, r.text
    assert r.json()["closed_at"] is not None
    assert r.json()["regulator"]["status"] == "not_sent"
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=100)) == []
    ev = db_session.get(Event, warning.event_id)
    db_session.refresh(ev)
    assert ev.state == "acknowledged"
    log = db_session.execute(select(AuditLog).where(AuditLog.action == "breach.close")).scalar_one()
    assert log.detail["outstanding_notices"] == ["regulator", "individuals"]
    assert client.post(f"/sites/{SITE}/breaches/{row.breach_id}/close", json={}).status_code == 409


def test_a_tick_only_sees_its_own_site(db_session: Session) -> None:
    db_session.add(Site(site_id="other-site", name="Other", timezone="Africa/Harare"))
    db_session.commit()
    _record(db_session, site_id="other-site")
    assert check_breach_deadlines(db_session, SITE, now=T0 + timedelta(hours=30)) == []
    assert len(check_breach_deadlines(db_session, "other-site", now=T0 + timedelta(hours=30))) == 1


# --- People affected ---------------------------------------------------------------------


def test_the_affected_people_export_is_audited_and_safe_to_open(
    client: TestClient, db_session: Session
) -> None:
    a = _operator(db_session, "op-1", name='=HYPERLINK("http://x","click")')
    b = _operator(db_session, "op-2", name="Bongani")
    erased = _operator(db_session, "op-3", name=None)
    db_session.get(Operator, erased).erased_at = T0  # type: ignore[union-attr]
    db_session.commit()
    row = _record(db_session, operator_ids=[b, a, erased])

    r = client.get(f"/sites/{SITE}/breaches/{row.breach_id}/affected.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["operator_id"] for x in rows] == ["op-1", "op-2", "op-3"]
    # A spreadsheet must not run an admin-entered name as a formula (CWE-1236).
    assert rows[0]["display_name"].startswith("'=")
    assert rows[2]["erased"] == "true" and rows[2]["display_name"] == ""
    log = db_session.execute(
        select(AuditLog).where(AuditLog.action == "personal_data.export")
    ).scalar_one()
    assert log.entity_type == "breach" and log.entity_id == row.breach_id
    assert log.detail == {"operators": 3}


# --- Access ------------------------------------------------------------------------------


@pytest.mark.parametrize("auth", [VIEWER, SUPERVISOR])
def test_only_admins_reach_the_register(db_session: Session, auth: tuple[str, str]) -> None:
    row = _record(db_session)
    c = make_client(db_session, auth)
    assert c.get(f"/sites/{SITE}/breaches").status_code == 403
    assert c.get(f"/sites/{SITE}/breaches/{row.breach_id}/affected.csv").status_code == 403


def test_a_site_admin_cannot_reach_another_sites_breaches(db_session: Session) -> None:
    from minemonitor.auth.service import create_user

    row = _record(db_session)
    create_user(
        db_session, username="other-admin", password="testpass123", role="admin", site_id="x"
    )
    db_session.commit()
    other = make_client(db_session, ("other-admin", "testpass123"))
    assert other.get(f"/sites/{SITE}/breaches").status_code == 403
    assert other.get(f"/sites/x/breaches/{row.breach_id}").status_code == 404


def test_the_register_lists_open_breaches_newest_first(
    client: TestClient, db_session: Session
) -> None:
    first = _record(db_session, discovered_at=T0)
    second = _record(db_session, discovered_at=T0 + timedelta(hours=1), now=T0 + timedelta(hours=3))
    closed = _record(db_session, discovered_at=T0 + timedelta(hours=2), now=T0 + timedelta(hours=3))
    client.post(f"/sites/{SITE}/breaches/{closed.breach_id}/close", json={})
    ids = [b["breach_id"] for b in client.get(f"/sites/{SITE}/breaches").json()]
    assert ids == [second.breach_id, first.breach_id]
    every = client.get(f"/sites/{SITE}/breaches", params={"state": "all"}).json()
    assert len(every) == 3
    assert every[0]["summary"] == SUMMARY


# --- Retention ---------------------------------------------------------------------------


def test_closed_breaches_follow_the_audit_window_and_open_ones_are_kept(
    db_session: Session,
) -> None:
    _operator(db_session, "op-1")
    old_open = _record(db_session, operator_ids=["op-1"])
    old_closed = _record(db_session, operator_ids=["op-1"])
    old_closed.closed_at = T0 + timedelta(days=1)
    recent_closed = _record(db_session)
    recent_closed.closed_at = T0 + timedelta(days=700)
    db_session.commit()

    deleted = run_retention(
        db_session,
        now=T0 + timedelta(days=740),
        positions_days=0,
        metrics_days=0,
        events_days=0,
        audit_days=730,
    )
    assert deleted["breach_records"] == 1
    left = set(db_session.execute(select(BreachRecord.breach_id)).scalars())
    assert left == {old_open.breach_id, recent_closed.breach_id}
    links = set(db_session.execute(select(BreachOperator.breach_id)).scalars())
    assert links == {old_open.breach_id}
    # Keep forever (0) deletes nothing.
    assert (
        run_retention(
            db_session,
            now=T0 + timedelta(days=9999),
            positions_days=0,
            metrics_days=0,
            events_days=0,
            audit_days=0,
        )["breach_records"]
        == 0
    )


# --- Compliance pack ---------------------------------------------------------------------


def test_the_compliance_pack_holds_what_an_inspector_asks_for(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import zipfile

    monkeypatch.setenv("MM_NOTIFY_WEBHOOK_URL", "https://hooks.example.org/T0/secret-token?k=v")
    monkeypatch.setenv("MM_NOTIFY_WHATSAPP_URL", "https://graph.facebook.com/v20.0/123/messages")
    monkeypatch.setenv("MM_RETAIN_POSITIONS_DAYS", "45")
    get_settings.cache_clear()
    op = _operator(db_session, "op-1", name="Alice Driver")
    breach = _record(db_session, operator_ids=[op])

    r = client.get(f"/sites/{SITE}/compliance-pack.zip")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == [
        "README.txt",
        "audit_log.csv",
        "breaches.csv",
        "processing.json",
        "retention.json",
    ]
    everything = b"".join(zf.read(n) for n in zf.namelist()).decode()
    # No personal data of the people in operators, and no secret from a configured URL.
    assert "Alice" not in everything and "alice@example.org" not in everything
    assert "secret-token" not in everything

    processing = json.loads(zf.read("processing.json"))
    assert processing["outside_recipients"] == [
        {
            "purpose": "alarm notifications (webhook): the alarm's event.v1",
            "host": "hooks.example.org",
        },
        {
            "purpose": "alarm notifications (WhatsApp): alarm summary and severity",
            "host": "graph.facebook.com",
        },
    ]
    assert processing["personal_data"]["operators"] == 1
    assert processing["breach_notice"]["regulator_notice_h"] == 24
    retention = json.loads(zf.read("retention.json"))
    assert retention["days_kept_by_class"]["positions"] == 45

    breaches_rows = list(csv.DictReader(io.StringIO(zf.read("breaches.csv").decode())))
    assert [b["breach_id"] for b in breaches_rows] == [breach.breach_id]
    assert breaches_rows[0]["people_affected"] == "1"
    audit_rows = list(csv.DictReader(io.StringIO(zf.read("audit_log.csv").decode())))
    assert "breach.record" in {a["action"] for a in audit_rows}

    log = db_session.execute(
        select(AuditLog).where(AuditLog.action == "compliance.pack_export")
    ).scalar_one()
    assert log.site_id == SITE and log.detail == {"audit_entries": len(audit_rows), "breaches": 1}


def test_the_compliance_pack_is_for_admins_of_an_existing_site(db_session: Session) -> None:
    assert (
        make_client(db_session, VIEWER).get(f"/sites/{SITE}/compliance-pack.zip").status_code == 403
    )
    admin = make_client(db_session, ("admin", "testpass123"))
    assert admin.get("/sites/nowhere/compliance-pack.zip").status_code == 404
    r = admin.post(
        "/sites/nowhere/breaches",
        json={
            "discovered_at": "2026-09-05T06:00:00Z",
            "summary": SUMMARY,
            "data_classes": ["other"],
        },
    )
    assert r.status_code == 404
