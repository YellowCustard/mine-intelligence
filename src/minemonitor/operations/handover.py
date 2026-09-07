"""Shift handover — the outgoing crew records what happened, the incoming crew
acknowledges it.

A handover is an operational record (annotation), stored apart from telemetry. Its
``summary`` is a frozen snapshot of the shift scorecard at the moment of handover —
the record of what was handed over — while the live scorecard remains separately
recomputable. Lifecycle is two steps: ``open`` (written by the outgoing supervisor)
→ ``acknowledged`` (by the incoming supervisor). Persistence helpers add rows but
do not commit; the caller commits so the write and its audit entry are atomic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.operations.scorecard import compute_scorecard
from minemonitor.operations.shifts import ShiftWindow
from minemonitor.storage.models import ShiftHandover


def create_handover(
    session: Session,
    *,
    site_id: str,
    window: ShiftWindow,
    outgoing_by: str,
    outgoing_notes: str | None = None,
    now: datetime | None = None,
) -> ShiftHandover:
    """Open a handover for a shift, snapshotting its scorecard. Caller commits."""
    # Freeze a JSON-safe snapshot (datetimes → ISO strings) into the record.
    summary = jsonable_encoder(compute_scorecard(session, site_id, window))
    row = ShiftHandover(
        id=str(ULID()),
        site_id=site_id,
        shift_id=window.shift_id,
        state="open",
        summary=summary,
        outgoing_by=outgoing_by,
        outgoing_notes=outgoing_notes,
        created_at=now or datetime.now(UTC),
    )
    session.add(row)
    return row


def get_handover(session: Session, site_id: str, handover_id: str) -> ShiftHandover | None:
    row = session.get(ShiftHandover, handover_id)
    if row is None or row.site_id != site_id:
        return None
    return row


def list_handovers(
    session: Session, site_id: str, *, shift_id: str | None = None, limit: int = 100
) -> list[ShiftHandover]:
    """Handovers for a site (always site-scoped), newest first."""
    stmt = select(ShiftHandover).where(ShiftHandover.site_id == site_id)
    if shift_id is not None:
        stmt = stmt.where(ShiftHandover.shift_id == shift_id)
    stmt = stmt.order_by(ShiftHandover.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def acknowledge_handover(
    session: Session,
    handover: ShiftHandover,
    *,
    incoming_by: str,
    incoming_notes: str | None = None,
    now: datetime | None = None,
) -> ShiftHandover:
    """Incoming crew acknowledges a handover. Raises ``ValueError`` if already done."""
    if handover.state != "open":
        raise ValueError("handover already acknowledged")
    handover.state = "acknowledged"
    handover.incoming_by = incoming_by
    handover.incoming_notes = incoming_notes
    handover.acknowledged_at = now or datetime.now(UTC)
    return handover


def as_dict(row: ShiftHandover) -> dict[str, Any]:
    return {
        "id": row.id,
        "site_id": row.site_id,
        "shift_id": row.shift_id,
        "state": row.state,
        "summary": row.summary,
        "outgoing_by": row.outgoing_by,
        "outgoing_notes": row.outgoing_notes,
        "created_at": row.created_at,
        "incoming_by": row.incoming_by,
        "incoming_notes": row.incoming_notes,
        "acknowledged_at": row.acknowledged_at,
    }
