"""User creation and authentication (with lockout and a verify cache)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth import cache, lockout
from minemonitor.auth.hashing import hash_password, verify_password
from minemonitor.config import get_settings
from minemonitor.storage.models import User

ROLES = ("viewer", "supervisor", "admin", "device")


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: str,
    site_id: str | None = None,
) -> User:
    """Create (or replace) a user. Caller commits."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    user = session.get(User, username)
    if user is None:
        user = User(username=username, created_at=datetime.now(UTC))
        session.add(user)
    user.password_hash = hash_password(password)
    user.role = role
    user.site_id = site_id
    cache.invalidate(username)  # a changed password must not stay cached-valid
    lockout.clear(session, username)  # an admin resetting credentials unlocks the account
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """Return the user if the password matches and the account is not locked.

    Order matters: an unknown user is rejected without creating lockout/audit
    rows (so username-spraying can't grow the tables); a locked account is
    rejected before any password work; a recent identical credential skips the
    PBKDF2 derivation via the verify cache; only a genuine failed password is
    counted, possibly locking the account, and audited.
    """
    settings = get_settings()
    user = session.get(User, username)
    if user is None:
        return None
    if lockout.is_locked(session, username):
        return None
    # Cache hit skips PBKDF2; lockout was already checked above.
    if cache.check(username, password, user.password_hash):
        return user
    if verify_password(password, user.password_hash):
        cache.store(username, password, user.password_hash, ttl_s=settings.auth_verify_cache_ttl_s)
        lockout.clear(session, username)
        session.commit()
        return user
    locked = lockout.record_failure(
        session,
        username,
        max_failures=settings.login_max_failures,
        lockout_minutes=settings.login_lockout_minutes,
    )
    audit.record(
        session,
        actor=username,
        action="auth.locked" if locked else "auth.login_failed",
        entity_type="user",
        entity_id=username,
        site_id=user.site_id,
    )
    session.commit()
    return None


def user_count(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(User)).scalar_one())
