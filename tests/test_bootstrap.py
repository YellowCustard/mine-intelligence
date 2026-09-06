"""Bootstrap-admin lifespan logic (Phase 3 — previously untested).

Creates the first admin from env only when the users table is empty, so a fresh
box is reachable. Uses its own empty engine (the shared fixture seeds users).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.api import main
from minemonitor.config import get_settings
from minemonitor.storage.models import Base, User


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    yield
    get_settings.cache_clear()


def _empty_factory():
    engine = create_engine(
        "sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _count(factory) -> int:
    s = factory()
    try:
        return int(s.execute(select(func.count()).select_from(User)).scalar_one())
    finally:
        s.close()


def test_creates_admin_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _empty_factory()
    monkeypatch.setattr(main, "get_session_factory", lambda: factory)
    monkeypatch.setenv("MM_BOOTSTRAP_ADMIN_USER", "boot")
    monkeypatch.setenv("MM_BOOTSTRAP_ADMIN_PASSWORD", "bootpass123")
    get_settings.cache_clear()

    main._bootstrap_admin()
    assert _count(factory) == 1
    s = factory()
    assert s.get(User, "boot").role == "admin"
    s.close()

    # Idempotent: a second call does not create a duplicate.
    main._bootstrap_admin()
    assert _count(factory) == 1


def test_no_op_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _empty_factory()
    monkeypatch.setattr(main, "get_session_factory", lambda: factory)
    monkeypatch.delenv("MM_BOOTSTRAP_ADMIN_USER", raising=False)
    monkeypatch.delenv("MM_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()

    main._bootstrap_admin()
    assert _count(factory) == 0
