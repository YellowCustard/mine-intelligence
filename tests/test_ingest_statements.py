"""A live fix costs the same SQL round trips however many zones its site has (soak).

Zone processing fetched each zone's debounce state for the asset with a query of its own:
with the soak's six zones a fix cost 13 statements, and every zone added to a site added
one more to every fix of every asset. The MQTT ingestor stores fixes one at a time, so
that was most of what bounded how many machines it could keep up with.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.ingest.geo import offset_m
from minemonitor.ingest.service import PositionIngest, store_and_process
from minemonitor.storage.models import Asset, AssetZoneState, Base, Site
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

SITE = "kn-zw-01"
T0 = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
_HOME = (-17.8252, 31.0335)


def _site(zones: int) -> sessionmaker[Session]:
    """A fresh database with one truck and ``zones`` zones it never goes near."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
        s.flush()
        s.add(Asset(site_id=SITE, asset_id="HT-1", asset_class="haul_truck"))
        for i in range(zones):
            centre = offset_m(*_HOME, 5000, 1000 * i)  # 1 km apart, 5 km away
            upsert_zone(
                s,
                site_id=SITE,
                zone_id=f"z-{i}",
                name=f"Zone {i}",
                kind="generic",
                geometry=box_polygon(*centre, 100),
                rules={},
            )
        s.commit()
    return make


def _fix(k: int) -> PositionIngest:
    lat, lon = offset_m(*_HOME, 0, 10 * k)
    return PositionIngest(
        site_id=SITE,
        asset_id="HT-1",
        ts=T0 + timedelta(seconds=k),
        lat=lat,
        lon=lon,
        speed_kph=20.0,
    )


def _statements_per_fix(zones: int) -> int:
    """SQL statements one live fix costs, once the asset's zone states exist."""
    factory = _site(zones)
    with factory() as s:
        store_and_process(s, _fix(0))  # the asset's first fix creates its zone states
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        with factory() as s:
            created, _events = store_and_process(s, _fix(1))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    with factory() as s:
        assert created and s.query(AssetZoneState).count() == zones
    engine.dispose()
    return len(seen)


def test_a_fix_costs_the_same_statements_with_six_zones_as_with_one() -> None:
    assert _statements_per_fix(6) == _statements_per_fix(1)
