"""Access-control domain service (FP-07). Callers commit unless noted.

Two responsibilities, kept separate:

- **Record** — ``record_access_event`` stores a gate/turnstile decision idempotently. It is a
  record only; the gate hardware enforces (Mine Monitor is advisory, brief §15). **No
  biometric template or image ever** (brief §4) — ``credential_ref`` is opaque and identity is
  a soft reference to ``operators``.
- **Authorise** — ``authorize`` applies Mine Monitor's own rules (suspended / not-inducted /
  off-shift) over an event. When the gate *granted* entry to someone those rules would reject,
  ``ingest_access_event`` raises a critical ``access_denied`` ``event.v1`` — the intelligence
  on top of the record: an unauthorised entry the gate let through.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.operations.shifts import resolve_shift
from minemonitor.storage.models import AccessEvent, Operator

_VALID_DECISIONS = {"granted", "denied"}
SOURCE = "access_gate"

# Authorisation verdicts (Mine Monitor's own conclusion, distinct from the gate's decision).
# "unknown" means we cannot authorise (no/unregistered operator) — recorded, never alarmed.
Verdict = str  # "granted" | "denied" | "unknown"


def _access_id(site_id: str, source_system: str, source_event_id: str) -> str:
    """Deterministic primary key, so replay/backfill of the same event is idempotent."""
    return f"access-{site_id}-{source_system}-{source_event_id}"


def record_access_event(
    session: Session,
    *,
    site_id: str,
    source_system: str,
    source_event_id: str,
    gate_id: str,
    decision: str,
    ts: datetime,
    now: datetime,
    credential_ref: str | None = None,
    operator_ref: str | None = None,
    reason: str | None = None,
    search_selected: bool = False,
    search_completed: bool | None = None,
) -> tuple[AccessEvent, bool]:
    """Store one access decision. Returns (row, created). Idempotent per source event.

    Rejects an unknown decision loudly (never coerced). A ``source_event_id`` already present
    for this (site, source_system) is returned untouched with ``created=False`` — a re-import
    never double-records or double-alarms.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(_VALID_DECISIONS)}")
    access_id = _access_id(site_id, source_system, source_event_id)
    existing = session.get(AccessEvent, access_id)
    if existing is not None:
        return existing, False
    row = AccessEvent(
        id=access_id,
        site_id=site_id,
        ts=ts,
        source_system=source_system,
        gate_id=gate_id,
        credential_ref=credential_ref,
        operator_ref=operator_ref,
        decision=decision,
        reason=reason,
        search_selected=search_selected,
        search_completed=search_completed,
        created_at=now,
    )
    session.add(row)
    return row, True


def authorize(
    session: Session, site_id: str, operator_ref: str | None, at: datetime
) -> tuple[Verdict, str | None]:
    """Mine Monitor's own access verdict for an operator at a time. No side effects.

    Denies a **suspended** or **not-inducted** operator, or one **off-shift** at ``at``
    (``resolve_shift`` returns None outside every shift window). An absent or unregistered
    operator yields ``unknown`` — we record but do not alarm what we cannot authorise.
    """
    if not operator_ref:
        return "unknown", "no operator reference"
    op = session.get(Operator, operator_ref)
    if op is None or op.site_id != site_id:
        return "unknown", "operator not registered"
    if op.erased_at is not None:
        return "unknown", "operator erased"
    if op.suspended:
        return "denied", "operator suspended"
    if not op.inducted:
        return "denied", "operator not inducted"
    if resolve_shift(session, site_id, at) is None:
        return "denied", "off-shift"
    return "granted", None


def ingest_access_event(
    session: Session,
    site_id: str,
    *,
    source_system: str,
    source_event_id: str,
    gate_id: str,
    decision: str,
    ts: datetime,
    now: datetime | None = None,
    credential_ref: str | None = None,
    operator_ref: str | None = None,
    reason: str | None = None,
    search_selected: bool = False,
    search_completed: bool | None = None,
) -> tuple[AccessEvent, bool, EventV1 | None]:
    """Record an access event and run authorisation over it. No commit (caller commits).

    Returns (row, created, alarm). When the event is newly recorded and the gate **granted**
    entry to someone Mine Monitor's rules would **deny**, a critical ``access_denied``
    ``event.v1`` is raised into the unified alarm queue — an unauthorised entry the gate let
    through. A duplicate (already recorded) never re-alarms.
    """
    now = now or datetime.now(UTC)
    row, created = record_access_event(
        session,
        site_id=site_id,
        source_system=source_system,
        source_event_id=source_event_id,
        gate_id=gate_id,
        decision=decision,
        ts=ts,
        now=now,
        credential_ref=credential_ref,
        operator_ref=operator_ref,
        reason=reason,
        search_selected=search_selected,
        search_completed=search_completed,
    )
    if not created:
        return row, False, None
    verdict, why = authorize(session, site_id, operator_ref, ts)
    alarm: EventV1 | None = None
    if verdict == "denied" and decision == "granted":
        alarm = EventV1(
            schema="event.v1",
            event_id=new_event_id(),
            site_id=site_id,
            ts=ts,
            type="access_denied",
            severity="critical",
            asset_id=None,
            zone_id=None,
            source=f"{SOURCE}:{gate_id}",
            summary=f"Unauthorised entry at {gate_id}: {why} (gate granted access)",
            detail={"gate_id": gate_id, "reason": why, "source_decision": decision},
            evidence={"access_event_id": row.id},
            advisory=True,
            state="open",
        )
        persist_event(session, alarm)
    return row, created, alarm


def set_operator_access_status(
    session: Session,
    site_id: str,
    operator_id: str,
    *,
    suspended: bool | None = None,
    inducted: bool | None = None,
) -> Operator | None:
    """Set an operator's access status (admin). Only provided flags change. No commit."""
    op = session.get(Operator, operator_id)
    if op is None or op.site_id != site_id:
        return None
    if suspended is not None:
        op.suspended = suspended
    if inducted is not None:
        op.inducted = inducted
    return op


def list_access_events(
    session: Session,
    site_id: str,
    *,
    gate_id: str | None = None,
    decision: str | None = None,
    limit: int = 200,
) -> list[AccessEvent]:
    """List access events for a site (always site-scoped), newest first."""
    stmt = select(AccessEvent).where(AccessEvent.site_id == site_id)
    if gate_id is not None:
        stmt = stmt.where(AccessEvent.gate_id == gate_id)
    if decision is not None:
        stmt = stmt.where(AccessEvent.decision == decision)
    stmt = stmt.order_by(AccessEvent.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())
