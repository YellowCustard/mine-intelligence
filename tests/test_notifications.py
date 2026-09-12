"""Notification egress: outbox enqueue, severity gating, retry/backoff, PII safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.config import Settings
from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.notifications import outbox
from minemonitor.notifications.dispatch import dispatch_pending, event_payload
from minemonitor.storage.models import Event, Notification
from tests.conftest import SUPERVISOR, VIEWER, make_client

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _naive(dt: datetime) -> datetime:
    # SQLite round-trips DateTime(timezone=True) as naive (Postgres keeps it aware);
    # normalise both sides so the tz artifact doesn't mask the value under test.
    return dt.replace(tzinfo=None)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "notify_min_severity": "warning",
        "notify_webhook_url": "http://hooks.local/mm",
        "notify_max_attempts": 3,
        "notify_retry_base_s": 60,
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _event(event_id: str = "e1", severity: str = "critical") -> EventV1:
    return EventV1(
        event_id=event_id,
        site_id="kn-zw-01",
        ts=_NOW,
        type="zone_breach",
        severity=severity,  # type: ignore[arg-type]
        asset_id="LV-07",
        zone_id="r1-magazine",
        source="gnss_geofence",
        summary="LV-07 entered R1 magazine",
    )


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.webhooks: list[tuple[str, dict[str, Any]]] = []
        self.emails: list[tuple[str, str, str]] = []

    def webhook(self, url: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.webhooks.append((url, payload))

    def email(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.emails.append((to, subject, body))


# --- gating & enqueue --------------------------------------------------------


def test_qualifies_by_severity_order() -> None:
    assert outbox.qualifies("critical", "warning")
    assert outbox.qualifies("warning", "warning")
    assert not outbox.qualifies("info", "warning")
    assert not outbox.qualifies("critical", "")  # off by default


def test_enqueue_off_by_default(db_session: Session) -> None:
    # No settings override → real defaults → notifications off → no rows.
    persist_event(db_session, _event())
    db_session.commit()
    assert db_session.execute(select(func.count()).select_from(Notification)).scalar_one() == 0


def test_enqueue_creates_rows_per_channel_and_is_idempotent(db_session: Session) -> None:
    s = _settings(
        notify_smtp_host="smtp.local",
        notify_email_to="ops@mine.example, boss@mine.example",
    )
    ev = _event()
    # The event row must exist for the FK; persist without auto-enqueue by using a
    # settings-scoped enqueue directly.
    persist_event(db_session, ev)  # off-by-default settings → no rows yet
    added = outbox.enqueue_for_event(db_session, ev, settings=s, now=_NOW)
    db_session.commit()
    assert added == 3  # 1 webhook + 2 email recipients
    # Idempotent: a second enqueue for the same event adds nothing.
    assert outbox.enqueue_for_event(db_session, ev, settings=s, now=_NOW) == 0


def test_low_severity_event_not_enqueued(db_session: Session) -> None:
    s = _settings(notify_min_severity="critical")
    ev = _event(severity="warning")
    persist_event(db_session, ev)
    assert outbox.enqueue_for_event(db_session, ev, settings=s, now=_NOW) == 0


def test_payload_is_advisory_and_carries_no_operator_name(db_session: Session) -> None:
    ev = _event()
    persist_event(db_session, ev)
    db_session.commit()
    row = db_session.get(Event, "e1")
    assert row is not None
    payload = event_payload(row)
    assert payload["advisory"] is True and payload["schema"] == "event.v1"
    # event.v1 has no operator identity — only ids and the summary.
    assert "operator" not in payload and "acknowledged_by" not in payload


# --- dispatch: success, retry, permanent failure -----------------------------


def _queue(db_session: Session, s: Settings) -> None:
    ev = _event()
    persist_event(db_session, ev)
    outbox.enqueue_for_event(db_session, ev, settings=s, now=_NOW)
    db_session.commit()


def test_dispatch_marks_sent(db_session: Session) -> None:
    s = _settings()
    _queue(db_session, s)
    sender = _FakeSender()
    out = dispatch_pending(db_session, sender=sender, settings=s, now=_NOW)
    assert out == {"sent": 1, "retry": 0, "failed": 0}
    assert sender.webhooks and sender.webhooks[0][0] == "http://hooks.local/mm"
    n = db_session.execute(select(Notification)).scalars().one()
    assert n.state == "sent" and n.sent_at is not None


def test_dispatch_retries_then_fails_after_max_attempts(db_session: Session) -> None:
    s = _settings(notify_max_attempts=3, notify_retry_base_s=60)
    _queue(db_session, s)
    sender = _FakeSender(fail=True)

    # Attempt 1: retry scheduled +60s.
    out = dispatch_pending(db_session, sender=sender, settings=s, now=_NOW)
    assert out["retry"] == 1
    n = db_session.execute(select(Notification)).scalars().one()
    assert n.state == "pending" and n.attempts == 1
    assert _naive(n.next_attempt_at) == _naive(_NOW + timedelta(seconds=60))
    assert n.last_error == "boom"

    # Nothing due yet at _NOW (backed off).
    assert dispatch_pending(db_session, sender=sender, settings=s, now=_NOW)["retry"] == 0

    # Attempt 2 at +60s: retry +120s.
    t2 = _NOW + timedelta(seconds=60)
    dispatch_pending(db_session, sender=sender, settings=s, now=t2)
    n = db_session.execute(select(Notification)).scalars().one()
    assert n.attempts == 2 and _naive(n.next_attempt_at) == _naive(t2 + timedelta(seconds=120))

    # Attempt 3 at +180s: hits max_attempts → permanently failed.
    t3 = t2 + timedelta(seconds=120)
    out = dispatch_pending(db_session, sender=sender, settings=s, now=t3)
    assert out["failed"] == 1
    n = db_session.execute(select(Notification)).scalars().one()
    assert n.state == "failed" and n.attempts == 3


# --- API ---------------------------------------------------------------------


def test_supervisor_can_list_notifications(db_session: Session) -> None:
    s = _settings()
    _queue(db_session, s)
    c = make_client(db_session, SUPERVISOR)
    rows = c.get("/sites/kn-zw-01/notifications").json()
    assert len(rows) == 1 and rows[0]["channel"] == "webhook" and rows[0]["state"] == "pending"
    # last_error/target visible for operational triage; no cross-site leakage.
    assert c.get("/sites/other/notifications").json() == []


def test_viewer_cannot_list_notifications(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    assert c.get("/sites/kn-zw-01/notifications").status_code == 403
