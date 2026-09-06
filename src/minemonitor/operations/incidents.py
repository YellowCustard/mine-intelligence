"""Incident lifecycle — the operations state machine over an alarm.

An alarm (``Event``) records that something happened; an incident tracks what a
supervisor *does* about it. The raw ``Event`` is never mutated: an incident links
to it and carries its own lifecycle, so the alarm queue and the investigation
workflow stay independent (brief §6). Every mutation appends to an append-only
timeline (``IncidentNote``) for full traceability; the router also writes the
compliance audit log.

Pure lifecycle rules live at the top so the state machine is unit-testable without
a database; persistence helpers below add rows but never commit — the caller
commits so the mutation and its audit entry land in one transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import Incident, IncidentNote

# The lifecycle, in order. "closed" is terminal.
INCIDENT_STATES: tuple[str, ...] = (
    "open",
    "acknowledged",
    "investigating",
    "assigned",
    "resolved",
    "closed",
)
TERMINAL_STATES: frozenset[str] = frozenset({"closed"})

# Allowed forward (and a couple of backward) transitions. You may always jump
# ahead — acknowledging is optional — but you cannot leave a closed incident, and
# a resolved incident can only be closed or reopened for further investigation.
_ALLOWED: dict[str, frozenset[str]] = {
    "open": frozenset({"acknowledged", "investigating", "assigned", "resolved", "closed"}),
    "acknowledged": frozenset({"investigating", "assigned", "resolved", "closed"}),
    "investigating": frozenset({"assigned", "resolved", "closed"}),
    "assigned": frozenset({"investigating", "resolved", "closed"}),
    "resolved": frozenset({"closed", "investigating"}),  # reopen if the fix did not hold
    "closed": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """True if ``current -> target`` is a legal lifecycle move."""
    return target in _ALLOWED.get(current, frozenset())


class InvalidTransition(ValueError):
    """Raised when a requested lifecycle transition is not allowed."""


def create_incident(
    session: Session,
    *,
    site_id: str,
    summary: str,
    type_: str,
    severity: str,
    actor: str,
    asset_id: str | None = None,
    zone_id: str | None = None,
    event_id: str | None = None,
    now: datetime | None = None,
) -> Incident:
    """Open a new incident (state ``open``) and record its first timeline entry."""
    ts = now or datetime.now(UTC)
    incident = Incident(
        incident_id=str(ULID()),
        site_id=site_id,
        event_id=event_id,
        type=type_,
        severity=severity,
        asset_id=asset_id,
        zone_id=zone_id,
        summary=summary,
        state="open",
        created_by=actor,
        created_at=ts,
        updated_at=ts,
    )
    session.add(incident)
    _append(
        session,
        incident,
        actor=actor,
        kind="state_change",
        from_state=None,
        to_state="open",
        text=summary,
        now=ts,
    )
    return incident


def get_incident(session: Session, site_id: str, incident_id: str) -> Incident | None:
    row = session.get(Incident, incident_id)
    if row is None or row.site_id != site_id:
        return None
    return row


def list_incidents(
    session: Session,
    site_id: str,
    *,
    state: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[Incident]:
    """List incidents for a site (always site-scoped), newest first."""
    stmt = select(Incident).where(Incident.site_id == site_id)
    if state is not None:
        stmt = stmt.where(Incident.state == state)
    if severity is not None:
        stmt = stmt.where(Incident.severity == severity)
    stmt = stmt.order_by(Incident.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def timeline(session: Session, incident_id: str) -> list[IncidentNote]:
    """The append-only timeline for an incident, oldest first."""
    stmt = (
        select(IncidentNote)
        .where(IncidentNote.incident_id == incident_id)
        .order_by(IncidentNote.ts.asc())
    )
    return list(session.execute(stmt).scalars().all())


def add_note(session: Session, incident: Incident, *, actor: str, text: str) -> IncidentNote:
    """Append a free-text note to the timeline (no state change)."""
    note = _append(
        session, incident, actor=actor, kind="note", from_state=None, to_state=None, text=text
    )
    incident.updated_at = datetime.now(UTC)
    return note


def transition_incident(
    session: Session,
    incident: Incident,
    *,
    actor: str,
    to_state: str,
    note: str | None = None,
    assignee: str | None = None,
    resolution: str | None = None,
    resolution_category: str | None = None,
    now: datetime | None = None,
) -> Incident:
    """Move an incident to ``to_state``, appending a timeline entry.

    Raises :class:`InvalidTransition` if the move is illegal, or ``ValueError`` if
    required fields are missing (an ``assigned`` needs an assignee; a ``resolved``
    needs a resolution). The caller commits.
    """
    if to_state not in INCIDENT_STATES:
        raise InvalidTransition(f"unknown state {to_state!r}")
    if not can_transition(incident.state, to_state):
        raise InvalidTransition(f"cannot move from {incident.state!r} to {to_state!r}")

    if to_state == "assigned":
        target = assignee if assignee is not None else incident.assignee
        if not target:
            raise ValueError("assigning an incident requires an assignee")
        incident.assignee = target
    elif assignee is not None:
        incident.assignee = assignee

    ts = now or datetime.now(UTC)
    if to_state == "resolved":
        if not resolution:
            raise ValueError("resolving an incident requires a resolution")
        incident.resolution = resolution
        incident.resolution_category = resolution_category
        incident.resolved_at = ts
    if to_state == "closed":
        incident.closed_at = ts

    from_state = incident.state
    incident.state = to_state
    incident.updated_at = ts
    _append(
        session,
        incident,
        actor=actor,
        kind="state_change",
        from_state=from_state,
        to_state=to_state,
        text=note,
        now=ts,
    )
    return incident


def _append(
    session: Session,
    incident: Incident,
    *,
    actor: str,
    kind: str,
    from_state: str | None,
    to_state: str | None,
    text: str | None,
    now: datetime | None = None,
) -> IncidentNote:
    note = IncidentNote(
        id=str(ULID()),
        incident_id=incident.incident_id,
        site_id=incident.site_id,
        ts=now or datetime.now(UTC),
        actor=actor,
        kind=kind,
        from_state=from_state,
        to_state=to_state,
        text=text,
    )
    session.add(note)
    return note
