"""Test fixtures.

M1 tests exercise the contracts and the ingest→store→readback path. The path
test uses SQLite for portability in CI without TimescaleDB; the Postgres-specific
concerns (hypertable, ON CONFLICT via the named constraint) are covered by the
integration run against the compose stack.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.api.main import create_app
from minemonitor.auth.service import create_user
from minemonitor.storage.db import get_db
from minemonitor.storage.models import Asset, Base, Site

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"

# Seed credentials used across the API tests.
ADMIN = ("admin", "testpass123")
VIEWER = ("viewer", "testpass123")
SUPERVISOR = ("sup", "testpass123")
DEVICE = ("device", "testpass123")


@pytest.fixture
def db_session() -> Iterator[Session]:
    """An in-memory SQLite session with the schema created and seed rows + users."""
    # StaticPool + check_same_thread=False: one shared in-memory DB across the
    # fixture thread and the TestClient's request thread.
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    session.add(Site(site_id="kn-zw-01", name="Test Site", timezone="Africa/Harare"))
    session.add(Asset(asset_id="HT-102", site_id="kn-zw-01", asset_class="haul_truck"))
    session.commit()
    create_user(session, username=ADMIN[0], password=ADMIN[1], role="admin")
    create_user(session, username=VIEWER[0], password=VIEWER[1], role="viewer")
    create_user(session, username=SUPERVISOR[0], password=SUPERVISOR[1], role="supervisor")
    create_user(session, username=DEVICE[0], password=DEVICE[1], role="device")
    session.commit()
    try:
        yield session
    finally:
        session.close()


def make_client(db_session: Session, auth: tuple[str, str] | None) -> TestClient:
    """Build a TestClient bound to the test DB, optionally authenticated."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    c = TestClient(app)
    if auth is not None:
        c.auth = auth
    return c


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """Authenticated (admin) TestClient — admin passes every role and device check."""
    c = make_client(db_session, ADMIN)
    with c:
        yield c
    c.app.dependency_overrides.clear()
