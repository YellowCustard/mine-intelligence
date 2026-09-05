"""Postgres integration tests — the dialect the mine actually runs.

Skipped unless ``MM_TEST_DATABASE_URL`` points at a reachable PostgreSQL. These
guard against dialect-specific bugs the SQLite suite cannot see — notably that
``INSERT ... ON CONFLICT DO NOTHING`` reports insertion correctly (psycopg3
returns rowcount -1 for it, so the idempotency signal must come from RETURNING).

Run locally against a throwaway database:
    MM_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/db \
        uv run pytest tests/test_ingest_postgres.py
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.contracts import AssetPositionV1
from minemonitor.storage.models import Asset, Base, Site
from minemonitor.storage.repositories import insert_position, list_positions

_PG_URL = os.environ.get("MM_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _PG_URL or not _PG_URL.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run integration tests",
)


@pytest.fixture
def pg_session() -> Iterator[Session]:
    engine = create_engine(_PG_URL, future=True)
    # Isolate from any existing schema; create_all covers the ORM tables (the
    # hypertable conversion is a migration concern, not needed for this logic).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    # Commit the site before the asset so the FK target is present.
    session.add(Site(site_id="kn-zw-01", name="Test", timezone="Africa/Harare"))
    session.commit()
    session.add(Asset(asset_id="HT-102", site_id="kn-zw-01", asset_class="haul_truck"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _position() -> AssetPositionV1:
    return AssetPositionV1(
        schema="asset.position.v1",
        site_id="kn-zw-01",
        asset_id="HT-102",
        ts=datetime(2026, 9, 5, 11, 42, 7, tzinfo=UTC),
        received_at=datetime.now(UTC),
        lat=-17.8252,
        lon=31.0335,
        source="pytest",
    )


def test_insert_reports_created_then_conflict(pg_session: Session) -> None:
    """First insert returns True; replay returns False; no duplicate row."""
    assert insert_position(pg_session, _position()) is True
    assert insert_position(pg_session, _position()) is False
    count = pg_session.execute(text("SELECT count(*) FROM positions")).scalar()
    assert count == 1


def test_read_back_is_site_scoped(pg_session: Session) -> None:
    insert_position(pg_session, _position())
    assert len(list_positions(pg_session, site_id="kn-zw-01")) == 1
    assert list_positions(pg_session, site_id="other") == []


def test_timestamp_round_trips_tz_aware(pg_session: Session) -> None:
    insert_position(pg_session, _position())
    row = list_positions(pg_session, site_id="kn-zw-01")[0]
    assert row.ts.tzinfo is not None
    assert row.received_at.tzinfo is not None
