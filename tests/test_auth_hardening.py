"""Auth hardening: failed-login lockout, the verify cache, and login auditing (Phase 2)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.auth import cache
from minemonitor.auth.service import authenticate, create_user
from minemonitor.storage.models import AuditLog, AuthLockout
from tests.conftest import ADMIN, make_client


def test_lockout_after_repeated_failures(db_session: Session) -> None:
    # Five wrong passwords (the default threshold) lock the account.
    for _ in range(5):
        assert authenticate(db_session, ADMIN[0], "wrong") is None
    row = db_session.get(AuthLockout, ADMIN[0])
    assert row is not None and row.failed_count >= 5 and row.locked_until is not None
    # While locked, even the correct password is refused.
    assert authenticate(db_session, ADMIN[0], ADMIN[1]) is None


def test_failures_and_lock_are_audited(db_session: Session) -> None:
    for _ in range(5):
        authenticate(db_session, ADMIN[0], "wrong")
    actions = {
        a.action
        for a in db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == ADMIN[0])
        ).scalars()
    }
    assert "auth.login_failed" in actions
    assert "auth.locked" in actions


def test_successful_login_is_not_audited(db_session: Session) -> None:
    # Basic auth re-sends creds on every request; a success is not an audit event.
    for _ in range(3):
        assert authenticate(db_session, ADMIN[0], ADMIN[1]) is not None
    logins = (
        db_session.execute(select(AuditLog).where(AuditLog.action.like("auth.%"))).scalars().all()
    )
    assert logins == []


def test_unknown_user_creates_no_lockout_row(db_session: Session) -> None:
    # Username-spraying must not grow the tables.
    assert authenticate(db_session, "ghost", "whatever") is None
    assert db_session.get(AuthLockout, "ghost") is None


def test_verify_cache_hits_and_invalidates_on_password_change(db_session: Session) -> None:
    from minemonitor.storage.models import User

    assert authenticate(db_session, ADMIN[0], ADMIN[1]) is not None
    # The credential is now cached (verified without re-deriving PBKDF2).
    admin = db_session.get(User, ADMIN[0])
    assert cache.check(ADMIN[0], ADMIN[1], admin.password_hash)
    # Changing the password invalidates the cached verification.
    create_user(db_session, username=ADMIN[0], password="new-password-123", role="admin")
    db_session.commit()
    assert not cache.check(ADMIN[0], ADMIN[1], admin.password_hash)


def test_credential_reset_clears_lockout(db_session: Session) -> None:
    for _ in range(5):
        authenticate(db_session, ADMIN[0], "wrong")
    assert db_session.get(AuthLockout, ADMIN[0]).locked_until is not None
    # An admin resetting the password unlocks the account.
    create_user(db_session, username=ADMIN[0], password="fresh-password-1", role="admin")
    db_session.commit()
    row = db_session.get(AuthLockout, ADMIN[0])
    assert row.locked_until is None and row.failed_count == 0
    assert authenticate(db_session, ADMIN[0], "fresh-password-1") is not None


def test_lockout_blocks_over_http(db_session: Session) -> None:
    bad = make_client(db_session, (ADMIN[0], "wrong"))
    for _ in range(5):
        assert bad.get("/me").status_code == 401
    # Account is now locked — the correct password is refused too.
    good = make_client(db_session, ADMIN)
    assert good.get("/me").status_code == 401
