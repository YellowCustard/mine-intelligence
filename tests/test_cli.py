"""The user-creation CLI (Phase 3 — previously untested)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.auth import cli
from minemonitor.auth.service import authenticate
from minemonitor.storage.models import User


def _point_cli_at(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)


def test_cli_creates_user_from_env_password(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_cli_at(db_session, monkeypatch)
    monkeypatch.setenv("MM_NEW_USER_PASSWORD", "cli-password-1")
    rc = cli.main(["alice", "supervisor"])
    assert rc == 0
    user = db_session.get(User, "alice")
    assert user is not None and user.role == "supervisor"
    assert authenticate(db_session, "alice", "cli-password-1") is not None


def test_cli_rejects_short_password(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_cli_at(db_session, monkeypatch)
    monkeypatch.setenv("MM_NEW_USER_PASSWORD", "short")
    assert cli.main(["bob", "viewer"]) == 2  # non-zero, no user created
    assert db_session.get(User, "bob") is None


def test_cli_site_scoped_user(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_cli_at(db_session, monkeypatch)
    monkeypatch.setenv("MM_NEW_USER_PASSWORD", "cli-password-1")
    assert cli.main(["ops", "viewer", "--site", "kn-zw-01"]) == 0
    assert db_session.get(User, "ops").site_id == "kn-zw-01"
