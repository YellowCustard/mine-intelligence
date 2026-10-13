"""Shift reports go out by email and WhatsApp after each shift (plan: later capability).

Each ended shift is queued once per recipient, after the delay that lets buffered fixes arrive,
with the shift's own figures and a note naming machines that had not reported to its end, and
sent with retry like alarms. Nothing personal leaves the mine: no operator name, no handover or
incident text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import compliance_pack, retention
from minemonitor.config import Settings
from minemonitor.ingest import tick
from minemonitor.notifications.dispatch import WhatsAppTemplate
from minemonitor.operations import shift_reports as reports
from minemonitor.operations.handover import create_handover
from minemonitor.operations.shifts import resolve_shift_by_id, windows_ending_between
from minemonitor.storage.models import (
    Event,
    HaulCycle,
    Incident,
    Operator,
    Position,
    ShiftReportDelivery,
)
from tests.conftest import SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
NIGHT = f"{SITE}:2026-10-12:night"  # 18:00 12 Oct to 06:00 13 Oct, Harare (16:00Z to 04:00Z)
DAY = f"{SITE}:2026-10-12:day"  # 06:00 to 18:00 12 Oct, Harare (04:00Z to 16:00Z)
NIGHT_END = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
_SAFE_DB = "postgresql+psycopg://mmuser:a-strong-secret@db.internal:5432/minemonitor"


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "notify_smtp_host": "smtp.mine.local",
        "notify_whatsapp_url": "https://graph.facebook.com/v20.0/1234/messages",
        "notify_whatsapp_token": "tok",
        "report_email_to": "manager@mine.example, planner@mine.example",
        "report_whatsapp_to": "+263771234567",
        "report_whatsapp_template": "mm_shift_report",
        "report_delay_s": 1800,
        "report_catchup_h": 24,
        "notify_max_attempts": 3,
        "notify_retry_base_s": 60,
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _rows(db: Session) -> list[ShiftReportDelivery]:
    return list(
        db.execute(
            select(ShiftReportDelivery).order_by(
                ShiftReportDelivery.shift_end,
                ShiftReportDelivery.channel,
                ShiftReportDelivery.target,
            )
        ).scalars()
    )


class _Sender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.emails: list[tuple[str, str, str]] = []
        self.whatsapps: list[tuple[str, str, WhatsAppTemplate | None]] = []

    def webhook(self, url: str, payload: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("reports never go to the webhook")

    def email(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise OSError("relay unreachable")
        self.emails.append((to, subject, body))

    def whatsapp(self, to: str, text: str, template: WhatsAppTemplate | None) -> None:
        if self.fail:
            raise OSError("relay unreachable")
        self.whatsapps.append((to, text, template))


def _cycle(asset: str, start: datetime, queue_s: float) -> HaulCycle:
    return HaulCycle(
        site_id=SITE,
        asset_id=asset,
        start_ts=start,
        end_ts=start + timedelta(minutes=20),
        cycle_time_s=1200.0,
        queue_s=queue_s,
        load_s=180.0,
        haul_s=420.0,
        dump_s=60.0,
        return_s=540.0 - queue_s,
    )


def _fix(asset: str, ts: datetime) -> Position:
    return Position(
        site_id=SITE,
        asset_id=asset,
        ts=ts,
        received_at=ts,
        lat=-17.8252,
        lon=31.0335,
        speed_kph=0.0,
        source="test",
    )


def _night_shift(db: Session) -> None:
    """Two cycles, three alarms, an incident and a handover with free text, two trucks."""
    t0 = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)  # 22:00 Harare, in the night shift
    db.add_all([_cycle("HT-102", t0, 240.0), _cycle("HT-103", t0, 120.0)])
    for i, sev in enumerate(("critical", "warning", "warning")):
        db.add(
            Event(
                event_id=f"ev{i}",
                site_id=SITE,
                ts=t0 + timedelta(minutes=i),
                type="zone_breach",
                severity=sev,
                source="gnss_geofence",
                summary="LV-07 entered R1 Explosives Magazine",
            )
        )
    db.add(
        Operator(
            operator_id="op-1",
            site_id=SITE,
            display_name="Tendai Moyo",
            created_at=t0,
        )
    )
    db.add(
        Incident(
            incident_id="inc-1",
            site_id=SITE,
            type="near_miss",
            severity="warning",
            summary="Tendai Moyo reported a near miss at the ramp",
            state="open",
            created_by="sup",
            created_at=t0,
            updated_at=t0,
        )
    )
    # HT-102 went quiet an hour before the end of the shift; HT-103 reported past it.
    db.add_all([_fix("HT-102", NIGHT_END - timedelta(hours=1)), _fix("HT-103", NIGHT_END)])
    window = resolve_shift_by_id(db, SITE, NIGHT)
    assert window is not None
    create_handover(
        db,
        site_id=SITE,
        window=window,
        outgoing_by="sup",
        outgoing_notes="Tendai Moyo on HT-102 again; call him about the ramp",
        now=NIGHT_END,
    )
    db.commit()


def test_shifts_that_cross_midnight_are_found_by_their_end(db_session: Session) -> None:
    windows = windows_ending_between(
        db_session, SITE, NIGHT_END - timedelta(hours=1), NIGHT_END + timedelta(hours=1)
    )
    assert [w.shift_id for w in windows] == [NIGHT]
    assert windows[0].end == NIGHT_END
    day_and_night = windows_ending_between(
        db_session, SITE, NIGHT_END - timedelta(hours=13), NIGHT_END
    )
    assert [w.shift_id for w in day_and_night] == [DAY, NIGHT]


def test_a_shift_is_queued_once_per_recipient_after_the_delay(db_session: Session) -> None:
    s = _settings(report_catchup_h=1)
    early = NIGHT_END + timedelta(seconds=1799)
    assert reports.queue_due_reports(db_session, SITE, settings=s, now=early) == 0

    due = NIGHT_END + timedelta(seconds=1800)
    assert reports.queue_due_reports(db_session, SITE, settings=s, now=due) == 3
    db_session.commit()
    rows = _rows(db_session)
    assert {(r.shift_id, r.channel, r.target) for r in rows} == {
        (NIGHT, "email", "manager@mine.example"),
        (NIGHT, "email", "planner@mine.example"),
        (NIGHT, "whatsapp", "+263771234567"),
    }
    # Every later tick, and a restart, finds them queued.
    for minutes in (1, 2, 30):
        later = due + timedelta(minutes=minutes)
        assert reports.queue_due_reports(db_session, SITE, settings=s, now=later) == 0
    # A recipient added later gets the shift too, and only they do.
    more = _settings(report_catchup_h=1, report_email_to="manager@mine.example, gm@mine.example")
    assert reports.queue_due_reports(db_session, SITE, settings=more, now=due) == 1


def test_after_an_outage_only_recent_shifts_are_reported(db_session: Session) -> None:
    # The ingestor was down for three days: the shifts of the last 24 h go out, not six.
    back = NIGHT_END + timedelta(days=3, hours=1)
    reports.queue_due_reports(db_session, SITE, settings=_settings(), now=back)
    shifts = {r.shift_id for r in _rows(db_session)}
    assert shifts == {f"{SITE}:2026-10-15:day", f"{SITE}:2026-10-15:night"}


def test_the_report_carries_the_shifts_figures_and_nothing_personal(db_session: Session) -> None:
    _night_shift(db_session)
    s = _settings(report_catchup_h=1, public_url="https://mm.mine.example/")
    reports.queue_due_reports(db_session, SITE, settings=s, now=NIGHT_END + timedelta(hours=1))
    db_session.commit()
    email = next(r for r in _rows(db_session) if r.channel == "email")

    assert email.subject == f"Shift report: {SITE}, Night shift 2026-10-12, 18:00 to 06:00"
    body = email.body
    assert "Haul cycles completed: 2" in body
    assert "Mean cycle time: 20.0 min" in body
    assert "Queue at the face: 15.0% of cycle time (observed)" in body  # 360 s of 2400 s
    assert "Alarms: 3 (critical 1, warning 2)" in body
    assert "Incidents opened: 1; still open: 1" in body
    assert "1 machine had not reported up to the end of the shift (HT-102)" in body
    assert "HT-103" not in body
    assert "Figures as of 2026-10-13 07:00 Africa/Harare." in body
    assert (
        "https://mm.mine.example/api/v1/sites/kn-zw-01/reports/shift.html"
        "?shift_id=kn-zw-01%3A2026-10-12%3Anight"
    ) in body
    assert "advisory" in body.lower()

    assert email.params == [
        SITE,
        "Night shift 2026-10-12, 18:00 to 06:00",
        "2",
        "20.0 min",
        "15.0%",
        "3",
        "1",
        "1 machine had not reported up to the end of the shift (HT-102): figures may still change.",
    ]
    assert len(email.params) == len(reports.TEMPLATE_PARAMS)
    # The operator's name is in the incident, the handover and the operators table; none of
    # it leaves in a report.
    for row in _rows(db_session):
        sent = row.subject + row.body + " ".join(row.params)
        assert "Tendai" not in sent and "Moyo" not in sent
        assert "ramp" not in sent and "near miss" not in sent


def test_an_empty_shift_reports_none_rather_than_zero(db_session: Session) -> None:
    reports.queue_due_reports(
        db_session, SITE, settings=_settings(report_catchup_h=1), now=NIGHT_END + timedelta(hours=1)
    )
    row = _rows(db_session)[0]
    assert "Haul cycles completed: 0" in row.body
    assert "Mean cycle time: none" in row.body
    assert "Queue at the face: none of cycle time" in row.body
    assert "Every machine that worked the shift reported to its end." in row.body


def test_queued_reports_are_sent_as_email_and_as_the_whatsapp_template(
    db_session: Session,
) -> None:
    _night_shift(db_session)
    s = _settings(report_catchup_h=1)
    now = NIGHT_END + timedelta(hours=1)
    reports.queue_due_reports(db_session, SITE, settings=s, now=now)
    db_session.commit()
    sender = _Sender()

    assert reports.send_due_reports(db_session, sender=sender, settings=s, now=now) == {
        "sent": 3,
        "retry": 0,
        "failed": 0,
    }
    assert sorted(to for to, _, _ in sender.emails) == [
        "manager@mine.example",
        "planner@mine.example",
    ]
    ((to, text, template),) = sender.whatsapps
    assert to == "+263771234567"
    assert template is not None
    assert template.name == "mm_shift_report" and template.language == "en"
    row = next(r for r in _rows(db_session) if r.channel == "whatsapp")
    assert list(template.params) == row.params
    assert text.startswith(f"Mine Monitor shift report for {SITE}, Night shift 2026-10-12")
    # SQLite drops the timezone on read; the stored instant is the same.
    assert {(r.state, r.sent_at and r.sent_at.replace(tzinfo=UTC)) for r in _rows(db_session)} == {
        ("sent", now)
    }
    # Sent once: the next tick has nothing to send.
    assert reports.send_due_reports(db_session, sender=sender, settings=s, now=now)["sent"] == 0


def test_without_a_template_whatsapp_gets_the_same_sentence_as_text(db_session: Session) -> None:
    s = _settings(report_catchup_h=1, report_whatsapp_template="", report_email_to="")
    now = NIGHT_END + timedelta(hours=1)
    reports.queue_due_reports(db_session, SITE, settings=s, now=now)
    db_session.commit()
    sender = _Sender()
    reports.send_due_reports(db_session, sender=sender, settings=s, now=now)
    ((_, text, template),) = sender.whatsapps
    assert template is None
    assert "0 haul cycles" in text and text.endswith("this system never controls plant.")


def test_a_failed_send_is_retried_with_backoff_then_marked_failed(db_session: Session) -> None:
    s = _settings(report_catchup_h=1, report_whatsapp_to="")
    now = NIGHT_END + timedelta(hours=1)
    reports.queue_due_reports(db_session, SITE, settings=s, now=now)
    db_session.commit()
    down = _Sender(fail=True)

    assert reports.send_due_reports(db_session, sender=down, settings=s, now=now)["retry"] == 2
    row = _rows(db_session)[0]
    assert row.attempts == 1 and row.last_error == "relay unreachable"
    assert row.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=60)
    # Not due yet: nothing is tried.
    assert reports.send_due_reports(db_session, sender=down, settings=s, now=now)["retry"] == 0
    t = now + timedelta(seconds=60)
    assert reports.send_due_reports(db_session, sender=down, settings=s, now=t)["retry"] == 2
    t += timedelta(seconds=120)
    assert reports.send_due_reports(db_session, sender=down, settings=s, now=t)["failed"] == 2
    assert {r.state for r in _rows(db_session)} == {"failed"}


def test_reports_are_off_without_recipients_or_without_their_transport(
    db_session: Session,
) -> None:
    now = NIGHT_END + timedelta(hours=1)
    off = _settings(report_email_to="", report_whatsapp_to="")
    assert reports.report_targets(off) == []
    assert reports.queue_due_reports(db_session, SITE, settings=off, now=now) == 0
    no_relay = _settings(notify_smtp_host="", notify_whatsapp_url="")
    assert reports.report_targets(no_relay) == []
    assert _rows(db_session) == []


def test_production_refuses_report_recipients_it_cannot_reach() -> None:
    prod: dict[str, Any] = {
        "env": "prod",
        "database_url": _SAFE_DB,
        "mqtt_username": "mm-ingestor",
    }
    with pytest.raises(ValueError, match="MM_REPORT_EMAIL_TO is set but MM_NOTIFY_SMTP_HOST"):
        Settings(_env_file=None, **prod, report_email_to="gm@mine.example")  # type: ignore[arg-type]
    wa = {**prod, "report_whatsapp_to": "+263771234567"}
    with pytest.raises(ValueError, match="MM_NOTIFY_WHATSAPP_URL is not"):
        Settings(_env_file=None, **wa)  # type: ignore[arg-type]
    wa["notify_whatsapp_url"] = "https://graph.facebook.com/v20.0/1234/messages"
    with pytest.raises(ValueError, match="MM_REPORT_WHATSAPP_TEMPLATE"):
        Settings(_env_file=None, **wa)  # type: ignore[arg-type]
    ok = Settings(_env_file=None, **wa, report_whatsapp_template="mm_shift_report")  # type: ignore[arg-type]
    assert ok.report_whatsapp_template == "mm_shift_report"


def test_the_tick_queues_reports_after_the_cycles_and_sends_after_alarms(
    db_session: Session,
) -> None:
    steps = tick.plan(
        db_session, _settings(), gate_source=None, notify_sender=_Sender(), retention_due=False
    )
    names = [(st.name, st.site_id) for st in steps]
    assert names.index(("shift_reports", SITE)) > names.index(("cycles", SITE))
    assert names.index(("report_delivery", None)) > names.index(("notifications", None))


def test_supervisors_see_the_report_outbox_and_viewers_do_not(db_session: Session) -> None:
    reports.queue_due_reports(
        db_session, SITE, settings=_settings(report_catchup_h=1), now=NIGHT_END + timedelta(hours=1)
    )
    db_session.commit()
    sup = make_client(db_session, SUPERVISOR)
    rows = sup.get(f"/sites/{SITE}/shift-report-deliveries").json()
    assert len(rows) == 3 and {r["shift_id"] for r in rows} == {NIGHT}
    assert sup.get(f"/sites/{SITE}/shift-report-deliveries?state=failed").json() == []
    assert (
        make_client(db_session, VIEWER).get(f"/sites/{SITE}/shift-report-deliveries").status_code
        == 403
    )


def test_retention_deletes_old_report_rows_with_the_events_class(db_session: Session) -> None:
    reports.queue_due_reports(
        db_session, SITE, settings=_settings(report_catchup_h=1), now=NIGHT_END + timedelta(hours=1)
    )
    db_session.commit()
    kept = retention.run_retention(
        db_session,
        now=NIGHT_END + timedelta(days=10),
        positions_days=0,
        metrics_days=0,
        events_days=30,
    )
    assert kept["shift_report_deliveries"] == 0
    gone = retention.run_retention(
        db_session,
        now=NIGHT_END + timedelta(days=31),
        positions_days=0,
        metrics_days=0,
        events_days=30,
    )
    assert gone["shift_report_deliveries"] == 3 and _rows(db_session) == []


def test_the_compliance_pack_lists_where_reports_go() -> None:
    purposes = {r["purpose"]: r["host"] for r in compliance_pack.recipients(_settings())}
    assert purposes["shift reports (email relay): shift figures and machine ids"] == (
        "smtp.mine.local"
    )
    assert purposes["shift reports (WhatsApp): shift figures and machine ids"] == (
        "graph.facebook.com"
    )
    off = compliance_pack.recipients(_settings(report_email_to="", report_whatsapp_to=""))
    assert not any(r["purpose"].startswith("shift reports") for r in off)
