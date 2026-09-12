"""Drain the notification outbox with retry and backoff (the delivery side).

Runs on the ingestor's maintenance tick. Each pending row due for delivery is sent
via its channel; success marks it ``sent``, a failure bumps the attempt count and
schedules an exponentially backed-off retry, and after ``notify_max_attempts`` the row
is marked ``failed`` so it stays visible in the queue instead of retrying forever.

The wire payload carries only ``event.v1`` fields — never an operator name (personal
data lives behind a foreign key, brief §4) — and always ``advisory: true`` (brief §15).
Delivery is done through a small :class:`Sender` protocol so tests inject a fake and
never touch the network.
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.config import Settings, get_settings
from minemonitor.storage.models import Event, Notification

log = logging.getLogger("minemonitor.notifications")


class Sender(Protocol):
    """Delivers a built notification over a channel. Raises on failure."""

    def webhook(self, url: str, payload: dict[str, Any]) -> None: ...

    def email(self, to: str, subject: str, body: str) -> None: ...


def event_payload(ev: Event) -> dict[str, Any]:
    """The advisory, PII-free payload for an event (event.v1 fields only)."""
    return {
        "schema": "event.v1",
        "event_id": ev.event_id,
        "site_id": ev.site_id,
        "ts": ev.ts.isoformat(),
        "type": ev.type,
        "severity": ev.severity,
        "asset_id": ev.asset_id,
        "zone_id": ev.zone_id,
        "source": ev.source,
        "summary": ev.summary,
        "advisory": True,
    }


class UrllibSmtpSender:
    """Default sender: urllib for webhooks, smtplib for email. Self-hostable."""

    def __init__(self, settings: Settings, *, timeout_s: float = 10.0) -> None:
        self._s = settings
        self._timeout = timeout_s

    def webhook(self, url: str, payload: dict[str, Any]) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - operator-configured URL
            if resp.status >= 300:
                raise RuntimeError(f"webhook returned {resp.status}")

    def email(self, to: str, subject: str, body: str) -> None:
        s = self._s
        msg = EmailMessage()
        msg["From"] = s.notify_email_from or s.notify_smtp_user or "minemonitor@localhost"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(s.notify_smtp_host, s.notify_smtp_port, timeout=self._timeout) as smtp:
            if s.notify_smtp_starttls:
                smtp.starttls()
            if s.notify_smtp_user:
                smtp.login(s.notify_smtp_user, s.notify_smtp_password)
            smtp.send_message(msg)


def _deliver(sender: Sender, n: Notification, ev: Event) -> None:
    payload = event_payload(ev)
    if n.channel == "webhook":
        sender.webhook(n.target, payload)
    elif n.channel == "email":
        subject = f"[{ev.severity}] {ev.site_id}: {ev.summary}"
        body = "Advisory alert (this system warns; it does not control plant).\n\n" + json.dumps(
            payload, indent=2
        )
        sender.email(n.target, subject, body)
    else:  # pragma: no cover - guarded by enqueue
        raise RuntimeError(f"unknown channel {n.channel!r}")


def dispatch_pending(
    session: Session,
    *,
    sender: Sender,
    settings: Settings | None = None,
    now: datetime | None = None,
    batch: int = 100,
) -> dict[str, int]:
    """Deliver due pending notifications. Commits. Returns per-outcome counts."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    rows = (
        session.execute(
            select(Notification)
            .where(Notification.state == "pending", Notification.next_attempt_at <= now)
            .order_by(Notification.next_attempt_at)
            .limit(batch)
        )
        .scalars()
        .all()
    )
    result = {"sent": 0, "retry": 0, "failed": 0}
    for n in rows:
        ev = session.get(Event, n.event_id)
        try:
            if ev is None:
                raise RuntimeError("event no longer exists")
            _deliver(sender, n, ev)
        except Exception as exc:  # noqa: BLE001 - any delivery error is retryable
            n.attempts += 1
            n.last_error = str(exc)[:500]
            if n.attempts >= s.notify_max_attempts:
                n.state = "failed"
                result["failed"] += 1
                log.warning(
                    "notification failed permanently",
                    extra={"site_id": n.site_id, "channel": n.channel, "error": n.last_error},
                )
            else:
                backoff = s.notify_retry_base_s * (2 ** (n.attempts - 1))
                n.next_attempt_at = now + timedelta(seconds=backoff)
                result["retry"] += 1
            continue
        n.state = "sent"
        n.sent_at = now
        n.attempts += 1
        result["sent"] += 1
    session.commit()
    return result
