"""Failed-login lockout, crash-safe in the database (brief §3).

After ``max_failures`` consecutive failures an account is locked for
``lockout_minutes``. State lives in ``auth_lockout`` so a restart neither resets
an attacker's progress nor frees a locked account. Callers commit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from minemonitor.storage.models import AuthLockout


def is_locked(session: Session, username: str, *, now: datetime | None = None) -> bool:
    """True if the account is currently locked out."""
    now = now or datetime.now(UTC)
    row = session.get(AuthLockout, username)
    if row is None or row.locked_until is None:
        return False
    # SQLite returns tz-aware columns as naive; normalise before comparing.
    locked_until = row.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    return locked_until > now


def record_failure(
    session: Session,
    username: str,
    *,
    max_failures: int,
    lockout_minutes: int,
    now: datetime | None = None,
) -> bool:
    """Count a failed attempt; lock the account when the threshold is reached.

    Returns True if this failure caused (or extended) a lock.
    """
    now = now or datetime.now(UTC)
    row = session.get(AuthLockout, username)
    if row is None:
        row = AuthLockout(username=username, failed_count=0, updated_at=now)
        session.add(row)
    row.failed_count += 1
    row.updated_at = now
    locked = False
    if row.failed_count >= max_failures:
        row.locked_until = now + timedelta(minutes=lockout_minutes)
        locked = True
    return locked


def clear(session: Session, username: str) -> None:
    """Reset lockout state after a successful login. No-op if nothing to reset."""
    row = session.get(AuthLockout, username)
    if row is not None and (row.failed_count or row.locked_until is not None):
        row.failed_count = 0
        row.locked_until = None
        row.updated_at = datetime.now(UTC)
