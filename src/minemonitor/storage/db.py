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


def is_transient_db_error(exc: BaseException) -> bool:
    """True if ``exc`` means "the database is unreachable or busy — retry later".

    Connectivity loss, a dropped/invalidated connection, pool exhaustion, lock or
    serialization conflicts (all :class:`OperationalError` / :class:`InterfaceError`
    in SQLAlchemy's DBAPI mapping) are transient. Anything else — constraint or data
    errors, and plain Python exceptions from processing code — will fail identically
    on every retry, so callers must not loop on it.
    """
    from sqlalchemy import exc as sa_exc

    if isinstance(
        exc,
        sa_exc.OperationalError
        | sa_exc.InterfaceError
        | sa_exc.DisconnectionError
        | sa_exc.TimeoutError,
    ):
        return True
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, ConnectionError | TimeoutError)
