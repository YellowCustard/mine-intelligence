"""Notification outbox API — read the store-and-forward alert queue (supervisor+).

Lets a supervisor see what alerts were queued, delivered, retrying, or permanently
failed for a site — the operational view of notification egress. Read-only: the outbox
is driven by events and the dispatcher, never edited by hand.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.auth.deps import require_supervisor
from minemonitor.storage.db import get_db
from minemonitor.storage.models import Notification, User

router = APIRouter(tags=["notifications"])


@router.get("/sites/{site_id}/notifications")
def list_notifications(
    site_id: str,
    state: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_supervisor),
) -> list[dict[str, Any]]:
    """List queued notifications for a site (newest first). Optional state filter."""
    stmt = select(Notification).where(Notification.site_id == site_id)
    if state is not None:
        stmt = stmt.where(Notification.state == state)
    stmt = stmt.order_by(Notification.created_at.desc()).limit(min(limit, 500))
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": n.id,
            "event_id": n.event_id,
            "channel": n.channel,
            "target": n.target,
            "state": n.state,
            "attempts": n.attempts,
            "created_at": n.created_at,
            "next_attempt_at": n.next_attempt_at,
            "sent_at": n.sent_at,
            "last_error": n.last_error,
        }
        for n in rows
    ]
