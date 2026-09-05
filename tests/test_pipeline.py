"""M3 acceptance (fast, SQLite): one confirmed breach = one critical event;
boundary-hugging raises none."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.contracts.position import AssetPositionV1
from minemonitor.pipeline import process_position
from minemonitor.storage.models import Asset, Base, Event, Site
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

_MAG_LAT, _MAG_LON = -17.8240, 31.0290  # placeholder magazine centre


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    s.commit()
    s.add(Asset(asset_id="LV-07", site_id="kn-zw-01", asset_class="light_vehicle"))
    s.commit()
    upsert_zone(
        s,
        site_id="kn-zw-01",
        zone_id="r1-explosives-magazine",
        name="R1 Explosives Magazine",
        kind="restricted",
        geometry=box_polygon(_MAG_LAT, _MAG_LON, 40),
        rules={"authorized_classes": [], "severity": "critical"},
    )
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _pos(asset_id: str, lat: float, lon: float, i: int) -> AssetPositionV1:
    ts = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC) + timedelta(seconds=i)
    return AssetPositionV1(
        schema="asset.position.v1",
        site_id="kn-zw-01",
        asset_id=asset_id,
        ts=ts,
        received_at=ts,
        lat=lat,
        lon=lon,
        speed_kph=10.0,
        ignition=True,
        source="test",
    )


def _feed(session: Session, positions: list[AssetPositionV1]) -> None:
    for p in positions:
        process_position(session, p, created=True)
        session.commit()


def _critical_breaches(session: Session, asset_id: str) -> list[Event]:
    return list(
        session.execute(
            select(Event).where(
                Event.asset_id == asset_id,
                Event.type == "zone_breach",
                Event.severity == "critical",
            )
        )
        .scalars()
        .all()
    )


def test_one_breach_per_entry(session: Session) -> None:
    far = (_MAG_LAT + 0.01, _MAG_LON)  # ~1.1 km north, well outside
    inside = (_MAG_LAT, _MAG_LON)  # centre
    seq = [
        _pos("LV-07", *far, 0),
        _pos("LV-07", *inside, 1),  # 1st inside
        _pos("LV-07", *inside, 2),  # 2nd inside -> confirmed entry -> 1 event
        _pos("LV-07", *inside, 3),  # still inside -> no new event
        _pos("LV-07", *inside, 4),
        _pos("LV-07", *far, 5),  # 1st out
        _pos("LV-07", *far, 6),  # 2nd out -> confirmed exit
    ]
    _feed(session, seq)
    assert len(_critical_breaches(session, "LV-07")) == 1


def test_boundary_hugging_raises_none(session: Session) -> None:
    """Skirting the edge (one inside fix at a time) never confirms entry."""
    far = (_MAG_LAT + 0.01, _MAG_LON)
    inside = (_MAG_LAT, _MAG_LON)
    seq = []
    for i in range(30):
        pt = inside if i % 2 == 0 else far
        seq.append(_pos("LV-07", *pt, i))
    _feed(session, seq)
    assert len(_critical_breaches(session, "LV-07")) == 0


def test_re_entry_raises_a_second_event(session: Session) -> None:
    far = (_MAG_LAT + 0.01, _MAG_LON)
    inside = (_MAG_LAT, _MAG_LON)
    seq = [
        _pos("LV-07", *far, 0),
        _pos("LV-07", *inside, 1),
        _pos("LV-07", *inside, 2),  # entry 1
        _pos("LV-07", *far, 3),
        _pos("LV-07", *far, 4),  # exit
        _pos("LV-07", *inside, 5),
        _pos("LV-07", *inside, 6),  # entry 2
    ]
    _feed(session, seq)
    assert len(_critical_breaches(session, "LV-07")) == 2
