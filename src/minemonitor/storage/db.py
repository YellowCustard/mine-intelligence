"""Database engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.config import get_settings

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    """Lazily create the process-wide SQLAlchemy engine.

    Postgres is the production target; a ``sqlite://`` URL is also accepted for a
    quick local run, in which case one shared connection is used across the
    server's worker threads (SQLite forbids cross-thread use of a connection).
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            _engine = create_engine(
                url,
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Lazily create the session factory bound to the engine."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and always close it."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
