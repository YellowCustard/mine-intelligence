"""Haul cycles across every loading and unloading zone (audit M3).

Only the first ``loading`` and the first ``unloading`` zone fed the cycle analysis. At a
mine with a ROM pad and a waste dump, every cycle to the second dump was dropped, and a
second face's loads were invisible. Every zone of each kind now counts, and each cycle
records the zone it loaded at and the one it dumped at: ore to the ROM pad against waste
to the dump is what reconciliation needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.contracts import AssetPositionV1
from minemonitor.cycles import compute_cycles
from minemonitor.cycles import recompute as rc
from minemonitor.cycles.recompute import recompute, refresh
from minemonitor.ingest.adapters.simulator import FACE, ROM, WASTE
from minemonitor.ingest.geo import offset_m
from minemonitor.storage.models import Base, HaulCycle, Site
from minemonitor.storage.repositories import insert_position
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

SITE = "kn-zw-01"
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)

FACE_B = offset_m(FACE[0], FACE[1], 0, -300)  # a second face, west of the first
_OUT = offset_m(FACE[0], FACE[1], 0, 300)  # on the haul road, outside every zone

_FACE_G = box_polygon(FACE[0], FACE[1], 70)
_FACE_B_G = box_polygon(FACE_B[0], FACE_B[1], 70)
_ROM_G = box_polygon(ROM[0], ROM[1], 70)
_WASTE_G = box_polygon(WASTE[0], WASTE[1], 70)

Leg = tuple[tuple[float, float], float, int]


def _fix(t: datetime, at: tuple[float, float], kph: float) -> AssetPositionV1:
    return AssetPositionV1(
        site_id=SITE,
        asset_id="HT-1",
        ts=t,
        received_at=t,
        lat=at[0],
        lon=at[1],
        speed_kph=kph,
        source="test",
    )


def _track(legs: list[Leg], start: datetime = T0) -> list[AssetPositionV1]:
    """Fixes at 1 Hz: each leg is (where, speed_kph, seconds)."""
    t, out = start, []
    for at, kph, secs in legs:
        for _ in range(secs):
            out.append(_fix(t, at, kph))
            t += timedelta(seconds=1)
    return out


def _cycle(face: tuple[float, float], dump: tuple[float, float]) -> list[Leg]:
    return [
        (face, 0.0, 60),  # queue
        (face, 8.0, 5),  # spot
        (face, 0.0, 90),  # load
        (_OUT, 30.0, 300),  # haul
        (dump, 0.0, 40),  # dump
        (_OUT, 30.0, 300),  # return
    ]


# --- the state machine ---------------------------------------------------------------


def test_each_cycle_records_where_it_loaded_and_dumped() -> None:
    fixes = _track([*_cycle(FACE, ROM), *_cycle(FACE, WASTE), (FACE, 0.0, 30)])
    cycles = compute_cycles(
        "HT-1",
        fixes,
        load_zones=[("pit-face", _FACE_G)],
        dump_zones=[("rom-pad", _ROM_G), ("waste-dump", _WASTE_G)],
    )
    assert [(c.load_zone_id, c.dump_zone_id) for c in cycles] == [
        ("pit-face", "rom-pad"),
        ("pit-face", "waste-dump"),
    ]


def test_a_cycle_from_a_second_face_counts() -> None:
    fixes = _track([*_cycle(FACE, ROM), *_cycle(FACE_B, ROM), (FACE, 0.0, 30)])
    cycles = compute_cycles(
        "HT-1",
        fixes,
        load_zones=[("pit-face", _FACE_G), ("face-b", _FACE_B_G)],
        dump_zones=[("rom-pad", _ROM_G)],
    )
    assert [c.load_zone_id for c in cycles] == ["pit-face", "face-b"]
    assert [c.cycle_time_s for c in cycles] == [795.0, 795.0]


def test_moving_between_touching_load_zones_is_one_visit() -> None:
    # The line forms in one zone and the loader stands in the next: one visit, not two
    # (two would end the cycle at the second zone with no dump in between and drop it).
    # It is credited to the zone where the truck spent most of the visit.
    line = offset_m(FACE[0], FACE[1], -100, 0)  # just south of the face's box
    zones = [("pit-face", _FACE_G), ("face-line", box_polygon(line[0], line[1], 35))]
    legs: list[Leg] = [
        (line, 0.0, 60),  # queue in the line's zone
        (FACE, 8.0, 5),  # spot into the face's zone
        (FACE, 0.0, 90),  # load
        (_OUT, 30.0, 300),
        (ROM, 0.0, 40),
        (_OUT, 30.0, 300),
    ]
    fixes = _track([*legs, *legs, (line, 0.0, 30)])
    cycles = compute_cycles("HT-1", fixes, load_zones=zones, dump_zones=[("rom-pad", _ROM_G)])
    assert len(cycles) == 2
    assert all(c.queue_s == 60.0 and c.load_zone_id == "pit-face" for c in cycles)


# --- the stored cycles ---------------------------------------------------------------


def _session(*, waste: bool = True, face_b: bool = False) -> Session:
    engine = create_engine(
        "sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
    s.commit()
    zones = [("pit-face", "loading", _FACE_G), ("rom-pad", "unloading", _ROM_G)]
    if waste:
        zones.append(("waste-dump", "unloading", _WASTE_G))
    if face_b:
        zones.append(("face-b", "loading", _FACE_B_G))
    for zone_id, kind, geom in zones:
        upsert_zone(
            s, site_id=SITE, zone_id=zone_id, name=zone_id, kind=kind, geometry=geom, rules={}
        )
    s.commit()
    return s


def _store(s: Session, fixes: list[AssetPositionV1]) -> None:
    for p in fixes:
        insert_position(s, p, commit=False)
    s.commit()


def _stored(s: Session) -> list[tuple]:
    s.expire_all()
    return [
        (c.start_ts, c.cycle_time_s, c.load_zone_id, c.dump_zone_id)
        for c in s.execute(select(HaulCycle).order_by(HaulCycle.start_ts)).scalars()
    ]


_MIXED = [
    *_cycle(FACE, ROM),
    *_cycle(FACE, WASTE),
    *_cycle(FACE_B, WASTE),
    *_cycle(FACE, ROM),
    (FACE, 0.0, 30),
]


def test_recompute_stores_cycles_to_every_dump_and_from_every_face() -> None:
    s = _session(face_b=True)
    _store(s, _track(_MIXED))
    recompute(s, SITE)
    assert [row[2:] for row in _stored(s)] == [
        ("pit-face", "rom-pad"),
        ("pit-face", "waste-dump"),
        ("face-b", "waste-dump"),
        ("pit-face", "rom-pad"),
    ]


def test_ticks_across_several_zones_match_a_full_rebuild() -> None:
    s = _session(face_b=True)
    fixes = _track(_MIXED)
    for i in range(0, len(fixes), 97):
        _store(s, fixes[i : i + 97])
        refresh(s, SITE)
    incremental = _stored(s)
    recompute(s, SITE)
    assert incremental == _stored(s)
    assert len(incremental) == 4


def test_adding_a_second_dump_zone_changes_the_zone_signature() -> None:
    # The tick rebuilds after a load or dump zone edit only when the signature moves.
    s = _session(waste=False)
    before = rc._zones(s, SITE).sig
    upsert_zone(
        s,
        site_id=SITE,
        zone_id="waste-dump",
        name="W",
        kind="unloading",
        geometry=_WASTE_G,
        rules={},
    )
    s.commit()
    added = rc._zones(s, SITE).sig
    moved = box_polygon(WASTE[0], WASTE[1], 90)
    upsert_zone(
        s, site_id=SITE, zone_id="waste-dump", name="W", kind="unloading", geometry=moved, rules={}
    )
    s.commit()
    assert len({before, added, rc._zones(s, SITE).sig}) == 3


def test_adding_a_dump_zone_mid_cycle_keeps_the_cycle_in_progress() -> None:
    # The edit moves the signature, and the tick rebuilds from its cursor: the entry of
    # the cycle under way. That entry is still an entry under the new zones (the fix
    # before it is outside every load zone), so the cycle must still open there and be
    # stored once it closes, exactly as a full rebuild stores it.
    s = _session(waste=False)
    fixes = _track([*_cycle(FACE, ROM), *_cycle(FACE, WASTE), *_cycle(FACE, ROM), (FACE, 0.0, 30)])
    cut = 795 + 155 + 100  # the second cycle is loaded and on its way to the waste dump
    _store(s, fixes[:cut])
    refresh(s, SITE)
    upsert_zone(
        s,
        site_id=SITE,
        zone_id="waste-dump",
        name="W",
        kind="unloading",
        geometry=_WASTE_G,
        rules={},
    )
    s.commit()
    for i in range(cut, len(fixes), 97):
        _store(s, fixes[i : i + 97])
        refresh(s, SITE)
    incremental = _stored(s)
    assert [row[2:] for row in incremental] == [
        ("pit-face", "rom-pad"),
        ("pit-face", "waste-dump"),
        ("pit-face", "rom-pad"),
    ]
    recompute(s, SITE)
    assert incremental == _stored(s)
