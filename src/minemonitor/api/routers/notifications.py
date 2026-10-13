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
from minemonitor.storage.models import Notification, ShiftReportDelivery, User

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


@router.get("/sites/{site_id}/shift-report-deliveries")
def list_shift_report_deliveries(
    site_id: str,
    state: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_supervisor),
) -> list[dict[str, Any]]:
    """List queued shift reports for a site (newest shift first). Optional state filter."""
    stmt = select(ShiftReportDelivery).where(ShiftReportDelivery.site_id == site_id)
    if state is not None:
        stmt = stmt.where(ShiftReportDelivery.state == state)
    stmt = stmt.order_by(ShiftReportDelivery.shift_end.desc(), ShiftReportDelivery.channel).limit(
        min(limit, 500)
    )
    return [
        {
            "id": r.id,
            "shift_id": r.shift_id,
            "shift_end": r.shift_end,
            "channel": r.channel,
            "target": r.target,
            "subject": r.subject,
            "state": r.state,
            "attempts": r.attempts,
            "created_at": r.created_at,
            "next_attempt_at": r.next_attempt_at,
            "sent_at": r.sent_at,
            "last_error": r.last_error,
        }
        for r in db.execute(stmt).scalars()
    ]
