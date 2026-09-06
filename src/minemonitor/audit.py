"""Append-only audit logging (brief §4).

Records rule/zone changes, acknowledgements, retention runs, and access to
personal data (operator reads, exports and erasures). Callers pass an open
session; the row is added but not committed here, so the audit entry commits with
the action it records (or not at all).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import AuditLog


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    site_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Add an audit entry. Commits with the caller's transaction."""
    session.add(
        AuditLog(
            id=str(ULID()),
            ts=datetime.now(UTC),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            site_id=site_id,
            detail=detail,
        )
    )
