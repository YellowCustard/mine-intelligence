"""``latest_positions`` on PostgreSQL returns the newest fix per asset (audit M6).

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL. On Postgres the query walks
the primary key instead of grouping the whole history, so it is checked here against the
plain GROUP BY answer, across two sites that reuse an asset id.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.storage.models import Base, Position, Site
from minemonitor.storage.repositories import latest_positions

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)

T0 = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(_DB, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add_all([Site(site_id=sid, name=sid, timezone="Africa/Harare") for sid in ("a", "b")])
    s.commit()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _fix(site_id: str, asset_id: str, ts: datetime, lat: float) -> Position:
    return Position(site_id=site_id, asset_id=asset_id, ts=ts, received_at=ts, lat=lat, lon=31.0)


def test_newest_fix_per_asset_matches_a_group_by(session: Session) -> None:
    rows = []
    # Asset ids in no particular order, with gaps, and one id ("HT-1") on both sites.
    for i, asset_id in enumerate(["HT-9", "HT-1", "LV-07", "EX-2", "HT-10"]):
        for k in range(1 + 7 * i):
            rows.append(_fix("a", asset_id, T0 + timedelta(seconds=13 * k + i), -17.0 - k))
    for k in range(5):
        rows.append(_fix("b", "HT-1", T0 + timedelta(hours=1, seconds=k), -18.0 - k))
    # A late-arriving fix older than the newest one must not displace it.
    rows.append(_fix("a", "HT-9", T0 - timedelta(days=3), -99.0))
    session.add_all(rows)
    session.commit()

    want: dict[tuple[str, str], Position] = {}
    for r in rows:
        key = (r.site_id, r.asset_id)
        if key not in want or r.ts > want[key].ts:
            want[key] = r

    for site_id in ("a", "b"):
        got = {(p.site_id, p.asset_id): (p.ts, p.lat) for p in latest_positions(session, site_id)}
        assert got == {k: (v.ts, v.lat) for k, v in want.items() if k[0] == site_id}, site_id


def test_a_site_with_no_fixes_has_no_latest_positions(session: Session) -> None:
    assert latest_positions(session, "a") == []
