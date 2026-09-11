"""Enqueue outbound notifications for qualifying events (the outbox side).

Called from :func:`minemonitor.events.repository.persist_event`, so a notification is
written in the **same transaction** as the event that triggers it — it cannot be lost
if the process dies before the dispatcher runs. Enqueue is idempotent per
(event, channel, target): re-persisting an event never double-notifies.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.config import Settings, get_settings
from minemonitor.contracts import EventV1
from minemonitor.storage.models import Notification

# info < warning < critical. A notification fires when the event's severity is at or
# above the configured minimum.
_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def qualifies(event_severity: str, min_severity: str) -> bool:
    """True if ``event_severity`` is at or above ``min_severity`` (blank = never)."""
    if not min_severity:
        return False
    return _SEVERITY_ORDER.get(event_severity, -1) >= _SEVERITY_ORDER.get(min_severity, 99)


def channel_targets(s: Settings) -> list[tuple[str, str]]:
    """The configured (channel, target) destinations. Empty = notifications off."""
    out: list[tuple[str, str]] = []
    if s.notify_webhook_url:
        out.append(("webhook", s.notify_webhook_url))
    if s.notify_smtp_host and s.notify_email_to:
        for addr in s.notify_email_to.split(","):
            addr = addr.strip()
            if addr:
                out.append(("email", addr))
    return out


def enqueue_for_event(
    session: Session,
    event: EventV1,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    """Queue notifications for an event if it qualifies. Returns rows added (no commit).

    No-op — and therefore invisible to the rest of the system — unless a minimum
    severity and at least one channel are configured. The caller commits (the event
    and its notifications land together).
    """
    s = settings or get_settings()
    if not qualifies(event.severity, s.notify_min_severity):
        return 0
    targets = channel_targets(s)
    if not targets:
        return 0
    now = now or datetime.now(UTC)
    added = 0
    for channel, target in targets:
        exists = session.execute(
            select(Notification.id).where(
                Notification.event_id == event.event_id,
                Notification.channel == channel,
                Notification.target == target,
            )
        ).first()
        if exists:
            continue
        session.add(
            Notification(
                id=str(ULID()),
                site_id=event.site_id,
                event_id=event.event_id,
                channel=channel,
                target=target,
                state="pending",
                attempts=0,
                created_at=now,
                next_attempt_at=now,
            )
        )
        added += 1
    return added
