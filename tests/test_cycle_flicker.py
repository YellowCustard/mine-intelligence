"""A truck at a zone's edge that reads outside for a moment has not left (audit M12).

Cycles used raw point-in-polygon with no exit debounce. A truck queueing at the edge of
the face reads outside for a fix or two as GNSS jitters, and that split its visit in
two: the first part closed nothing (no dump since), and the cycle started at the second,
part-way through the queue, so the queue time this system exists to measure came out
short. A truck tipping at the edge of the dump lost part of its dump time the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minemonitor.contracts import AssetPositionV1
from minemonitor.cycles import Cycle, compute_cycles
from minemonitor.cycles.statemachine import MIN_EXIT_S
from minemonitor.ingest.adapters.simulator import FACE, ROM
from minemonitor.ingest.geo import offset_m
from minemonitor.zones.geometry import box_polygon

T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)
_LOADS = [("pit-face", box_polygon(FACE[0], FACE[1], 70))]
_DUMPS = [("rom-pad", box_polygon(ROM[0], ROM[1], 70))]
_OUT = offset_m(FACE[0], FACE[1], 0, 300)  # on the haul road, outside both zones

# 5 m either side of each zone's southern edge.
FACE_EDGE_IN = offset_m(FACE[0], FACE[1], -65, 0)
FACE_EDGE_OUT = offset_m(FACE[0], FACE[1], -75, 0)
ROM_EDGE_IN = offset_m(ROM[0], ROM[1], -65, 0)
ROM_EDGE_OUT = offset_m(ROM[0], ROM[1], -75, 0)

Leg = tuple[tuple[float, float], float, int]


def _track(legs: list[Leg]) -> list[AssetPositionV1]:
    """Fixes at 1 Hz: each leg is (where, speed_kph, seconds)."""
    t, out = T0, []
    for at, kph, secs in legs:
        for _ in range(secs):
            out.append(
                AssetPositionV1(
                    site_id="kn-zw-01",
                    asset_id="HT-1",
                    ts=t,
                    received_at=t,
                    lat=at[0],
                    lon=at[1],
                    speed_kph=kph,
                    source="test",
                )
            )
            t += timedelta(seconds=1)
    return out


def _cycles(legs: list[Leg]) -> list[Cycle]:
    return compute_cycles("HT-1", _track(legs), load_zones=_LOADS, dump_zones=_DUMPS)


def _haul(queue: list[Leg], dump: list[Leg]) -> list[Leg]:
    return [
        *queue,
        (FACE, 8.0, 5),  # spot
        (FACE, 0.0, 90),  # load
        (_OUT, 30.0, 300),  # haul
        *dump,
        (_OUT, 30.0, 300),  # return
    ]


_QUEUE = [(FACE_EDGE_IN, 0.0, 60)]
_DUMP = [(ROM, 0.0, 40)]


def test_a_queue_at_the_face_edge_that_reads_outside_is_one_visit() -> None:
    flicker = [(FACE_EDGE_IN, 0.0, 30), (FACE_EDGE_OUT, 0.0, 3), (FACE_EDGE_IN, 0.0, 27)]
    cycles = _cycles([*_haul(flicker, _DUMP), *_haul(flicker, _DUMP), (FACE_EDGE_IN, 0.0, 30)])
    # The cycle starts when the truck joined the line, and all 60 s of it is queue.
    assert [c.start_ts for c in cycles] == [T0, T0 + timedelta(seconds=795)]
    assert [c.queue_s for c in cycles] == [60.0, 60.0]
    assert [c.cycle_time_s for c in cycles] == [795.0, 795.0]


def test_tipping_at_the_dump_edge_that_reads_outside_is_one_dump() -> None:
    flicker = [(ROM_EDGE_IN, 0.0, 20), (ROM_EDGE_OUT, 0.0, 3), (ROM_EDGE_IN, 0.0, 17)]
    cycles = _cycles([*_haul(_QUEUE, flicker), *_haul(_QUEUE, flicker), (FACE, 0.0, 30)])
    assert [(c.haul_s, c.dump_s, c.return_s) for c in cycles] == [(300.0, 40.0, 300.0)] * 2


def test_a_flicker_matches_the_same_queue_read_cleanly() -> None:
    flicker = [(FACE_EDGE_IN, 0.0, 30), (FACE_EDGE_OUT, 0.0, 3), (FACE_EDGE_IN, 0.0, 27)]
    clean = _cycles([*_haul(_QUEUE, _DUMP) * 2, (FACE_EDGE_IN, 0.0, 30)])
    jittery = _cycles([*_haul(flicker, _DUMP) * 2, (FACE_EDGE_IN, 0.0, 30)])
    assert jittery == clean


def test_leaving_for_the_minimum_exit_is_two_visits() -> None:
    # Out for MIN_EXIT_S: it left. The first visit closes nothing (no dump since), and the
    # cycle starts at the second.
    away = int(MIN_EXIT_S)
    legs = [
        (FACE, 0.0, 60),
        (_OUT, 30.0, away),
        *_haul([(FACE, 0.0, 60)], _DUMP),
        (FACE, 0.0, 30),
    ]
    cycles = _cycles(legs)
    assert [c.start_ts for c in cycles] == [T0 + timedelta(seconds=60 + away)]


def test_brushing_the_zone_edge_on_the_way_past_is_not_a_visit() -> None:
    # In for 5 s, out for 3, in for 5: one run of 13 s, still shorter than a visit.
    brush = [(FACE_EDGE_IN, 30.0, 5), (FACE_EDGE_OUT, 30.0, 3), (FACE_EDGE_IN, 30.0, 5)]
    legs = [
        *_haul(_QUEUE, _DUMP)[:4],  # queue, spot, load, haul
        (_OUT, 30.0, 100),
        *brush,  # passing the face's edge on the way to the dump
        (_OUT, 30.0, 100),
        *_DUMP,
        (_OUT, 30.0, 300),
        (FACE, 0.0, 30),
    ]
    cycles = _cycles(legs)
    assert len(cycles) == 1 and cycles[0].start_ts == T0
