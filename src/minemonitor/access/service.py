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

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.operations.shifts import resolve_shift
from minemonitor.storage.models import AccessEvent, Event, Operator

_VALID_DECISIONS = {"granted", "denied"}
SOURCE = "access_gate"

# Authorisation verdicts (Mine Monitor's own conclusion, distinct from the gate's decision).
# "unknown" means we cannot authorise (no/unregistered operator) — recorded, never alarmed.
Verdict = str  # "granted" | "denied" | "unknown"


def _access_id(site_id: str, source_system: str, source_event_id: str) -> str:
    """Deterministic primary key, so replay/backfill of the same event is idempotent."""
    return f"access-{site_id}-{source_system}-{source_event_id}"


def _select_for_search(access_id: str, rate_percent: int) -> bool:
    """Deterministically select a passage for a physical search at ``rate_percent`` (0–100).

    Keyed on the (stable) access id, so the choice is reproducible and idempotent — replaying
    the same event selects it the same way, never a fresh coin flip. ``0`` disables it.
    """
    if rate_percent <= 0:
        return False
    bucket = int(hashlib.sha256(access_id.encode()).hexdigest(), 16) % 100
    return bucket < min(rate_percent, 100)


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
    search_rate_percent: int | None = None,
) -> tuple[AccessEvent, bool, EventV1 | None]:
    """Record an access event and run authorisation over it. No commit (caller commits).

    Returns (row, created, alarm). When the event is newly recorded and the gate **granted**
    entry to someone Mine Monitor's rules would **deny**, a critical ``access_denied``
    ``event.v1`` is raised into the unified alarm queue — an unauthorised entry the gate let
    through. A duplicate (already recorded) never re-alarms.

    If the source did not already flag the passage for a physical search, Mine Monitor's
    **random-search generator** may select it deterministically at the configured rate
    (``search_rate_percent``, default from settings) — recorded on the passage for the
    missed-search audit trail.
    """
    now = now or datetime.now(UTC)
    rate = (
        search_rate_percent
        if search_rate_percent is not None
        else get_settings().access_search_rate_percent
    )
    # Only a *granted* passage (someone who actually entered) is auto-selected for a physical
    # search; a denied attempt did not come in, so selecting it — and later escalating a missed
    # search — would be noise. An explicit source-provided selection is still honoured.
    effective_selected = search_selected or (
        decision == "granted"
        and _select_for_search(_access_id(site_id, source_system, source_event_id), rate)
    )
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
        search_selected=effective_selected,
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


def complete_search(
    session: Session,
    site_id: str,
    access_event_id: str,
    *,
    metal_detected: bool | None = None,
    now: datetime | None = None,
) -> tuple[AccessEvent | None, EventV1 | None]:
    """Record that a passage's physical search was completed. No commit (caller commits).

    Marks ``search_completed`` (stopping any future missed-search escalation) and records the
    metal-detector outcome — the detector's association with the gate passage. A positive
    detection raises a critical ``metal_detected`` ``event.v1`` (deduped per passage). Returns
    (row, alarm), or (None, None) if the passage is not found for this site.
    """
    now = now or datetime.now(UTC)
    row = session.get(AccessEvent, access_event_id)
    if row is None or row.site_id != site_id:
        return None, None
    row.search_completed = True
    if metal_detected is not None:
        row.metal_detected = metal_detected
    alarm: EventV1 | None = None
    if metal_detected and session.get(Event, f"metal-{row.id}") is None:
        candidate = EventV1(
            schema="event.v1",
            event_id=f"metal-{row.id}",
            site_id=site_id,
            ts=now,
            type="metal_detected",
            severity="critical",
            asset_id=None,
            zone_id=None,
            source=f"{SOURCE}:{row.gate_id}",
            summary=f"Metal detected in search at {row.gate_id}",
            detail={"gate_id": row.gate_id, "access_event_id": row.id},
            evidence={"access_event_id": row.id},
            advisory=True,
            state="open",
        )
        # Deterministic id + a savepoint'd flush: a concurrent completion of the same passage
        # collides on the primary key here (not at the caller's commit → no 500), and is
        # treated as the documented per-passage dedup rather than an error.
        try:
            with session.begin_nested():
                persist_event(session, candidate)
                session.flush()
            alarm = candidate
        except IntegrityError:
            alarm = None
    return row, alarm


def detect_missed_searches(
    session: Session,
    site_id: str,
    *,
    now: datetime | None = None,
    grace_s: int | None = None,
) -> list[EventV1]:
    """Escalate passages selected for search but never completed. Commits.

    A passage flagged ``search_selected`` whose search is still not completed ``grace_s`` after
    the passage time raises one ``search_missed`` ``event.v1`` (warning), deduped per passage —
    the "a selected search was skipped" alert (FP-07). Runs on the maintenance tick.
    """
    now = now or datetime.now(UTC)
    grace = grace_s if grace_s is not None else get_settings().access_search_grace_s
    cutoff = now - timedelta(seconds=grace)
    rows = (
        session.execute(
            select(AccessEvent).where(
                AccessEvent.site_id == site_id,
                AccessEvent.search_selected.is_(True),
                or_(
                    AccessEvent.search_completed.is_(None),
                    AccessEvent.search_completed.is_(False),
                ),
            )
        )
        .scalars()
        .all()
    )
    events: list[EventV1] = []
    for row in rows:
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC)
        if ts > cutoff:  # still within the grace window
            continue
        ev_id = f"search-missed-{row.id}"
        if session.get(Event, ev_id) is not None:
            continue  # already escalated (deduped)
        ev = EventV1(
            schema="event.v1",
            event_id=ev_id,
            site_id=site_id,
            ts=now,
            type="search_missed",
            severity="warning",
            asset_id=None,
            zone_id=None,
            source=f"{SOURCE}:{row.gate_id}",
            summary=f"Selected search not completed at {row.gate_id}",
            detail={"gate_id": row.gate_id, "access_event_id": row.id},
            evidence={"access_event_id": row.id},
            advisory=True,
            state="open",
        )
        persist_event(session, ev)
        events.append(ev)
    session.commit()
    return events
