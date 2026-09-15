"""WhatsApp notification channel: enqueue per recipient, dispatch, PII-free payload."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from minemonitor.config import Settings
from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.notifications import outbox
from minemonitor.notifications.dispatch import dispatch_pending

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SITE = "kn-zw-01"


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "notify_min_severity": "warning",
        "notify_whatsapp_url": "https://graph.example/v20.0/1/messages",
        "notify_whatsapp_token": "tok-123",
        "notify_whatsapp_to": "+263771234567, +263779999999",
        "notify_max_attempts": 3,
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _event() -> EventV1:
    return EventV1(
        event_id="wa1",
        site_id=SITE,
        ts=_NOW,
        type="zone_occupancy",
        severity="critical",
        asset_id=None,
        zone_id="sector-a",
        source="gnss_occupancy",
        summary="6 assets in Sector A exceeds capacity 5",
    )


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def webhook(self, url: str, payload: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("webhook not configured in this test")

    def email(self, to: str, subject: str, body: str) -> None:  # pragma: no cover
        raise AssertionError("email not configured in this test")

    def whatsapp(self, to: str, text: str) -> None:
        self.sent.append((to, text))


def test_whatsapp_enqueues_per_recipient_and_dispatches(db_session: Session) -> None:
    s = _settings()
    ev = _event()
    persist_event(db_session, ev)  # off-by-default env settings → no auto rows
    added = outbox.enqueue_for_event(db_session, ev, settings=s, now=_NOW)
    db_session.commit()
    assert added == 2  # one row per recipient number

    fake = _FakeSender()
    res = dispatch_pending(db_session, sender=fake, settings=s, now=_NOW)
    assert res["sent"] == 2
    recipients = {to for to, _ in fake.sent}
    assert recipients == {"+263771234567", "+263779999999"}
    # Advisory, PII-free: the message carries the summary + severity, never an operator name.
    _, text = fake.sent[0]
    assert "Sector A" in text and "critical" in text and "advisory" in text
    assert "operator" not in text.lower()


def test_whatsapp_off_unless_url_and_recipients_set(db_session: Session) -> None:
    # URL set but no recipients → no whatsapp rows.
    s = _settings(notify_whatsapp_to="")
    assert ("whatsapp", "") not in outbox.channel_targets(s)
    assert all(ch != "whatsapp" for ch, _ in outbox.channel_targets(s))
