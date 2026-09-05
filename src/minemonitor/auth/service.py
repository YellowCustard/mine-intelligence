"""User creation and authentication."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.auth.hashing import hash_password, verify_password
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
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """Return the user if the password matches, else None."""
    user = session.get(User, username)
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def user_count(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(User)).scalar_one())
