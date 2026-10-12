"""Queue time on hand-built tracks: what counts as waiting for the loader (audit M2).

A truck waits in line in steps (stop, creep forward a truck length, stop again) until
the loader calls it forward. Queue time runs from its first stop in the load zone to
that call: the start of its move onto the loading spot. These tracks pin the cases a
speed-only "first stop until first movement" rule got wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from minemonitor.cycles import compute_cycles
from minemonitor.cycles.statemachine import Cycle
from minemonitor.ingest.geo import offset_m
from minemonitor.zones.geometry import box_polygon

_BASE = (-17.8252, 31.0335)
_LOAD = box_polygon(*_BASE, 70)  # loader spot at 0 m; the line forms south of it
_DUMP_AT = offset_m(*_BASE, 400, 0)
_DUMP = box_polygon(*_DUMP_AT, 50)
T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Fix:
    ts: datetime
    lat: float
    lon: float
    speed_kph: float | None


class _Track:
    """A 1 Hz track along one north-south line through the loader spot."""

    def __init__(self, north_m: float) -> None:
        self.t = T0
        self.north_m = north_m
        self.fixes: list[_Fix] = []

    def _emit(self, speed_kph: float) -> None:
        lat, lon = offset_m(*_BASE, self.north_m, 0)
        self.fixes.append(_Fix(self.t, lat, lon, speed_kph))
        self.t += timedelta(seconds=1)

    def stop(self, seconds: int) -> _Track:
        for _ in range(seconds):
            self._emit(0.0)
        return self

    def drive(self, to_north_m: float, kph: float) -> _Track:
        step = kph / 3.6
        while abs(to_north_m - self.north_m) > 1e-9:
            d = to_north_m - self.north_m
            self.north_m = to_north_m if abs(d) <= step else self.north_m + step * (d / abs(d))
            self._emit(kph)
        return self

    def to_dump_and_back(self) -> _Track:
        """Leave the load zone, dump, and come back in (which closes the cycle)."""
        return self.drive(400, 30).stop(40).drive(150, 30).drive(0, 30).stop(60).drive(150, 30)


def _one_cycle(track: _Track) -> Cycle:
    cycles = compute_cycles("HT-1", track.fixes, load_geom=_LOAD, dump_geom=_DUMP)
    assert len(cycles) == 1
    return cycles[0]


def test_a_queue_that_moves_up_in_steps_counts_in_full() -> None:
    tr = _Track(150).drive(-45, 30)
    queue_start = tr.t
    tr.stop(60).drive(-30, 8).stop(60).drive(-15, 8).stop(40)
    called_forward = tr.t
    tr.drive(0, 8).stop(90).to_dump_and_back()

    c = _one_cycle(tr)

    assert c.queue_s == (called_forward - queue_start).total_seconds()  # 160 s + two creeps
    assert c.queue_s > 160


def test_a_truck_that_goes_straight_to_the_loader_has_no_queue() -> None:
    tr = _Track(150).drive(0, 20).stop(90).to_dump_and_back()

    c = _one_cycle(tr)

    assert c.queue_s == 0.0  # its only stop is the loading
    assert c.load_s > 90


def test_a_speed_spike_while_loading_does_not_split_the_loading() -> None:
    tr = _Track(150).drive(-15, 30)
    queue_start = tr.t
    tr.stop(60)
    called_forward = tr.t
    tr.drive(0, 8).stop(40)
    tr._emit(9.0)  # one fix of GNSS speed noise, standing still under the loader
    tr.stop(50).to_dump_and_back()

    c = _one_cycle(tr)

    assert c.queue_s == (called_forward - queue_start).total_seconds()


def test_a_short_stop_on_the_way_out_is_not_the_loading() -> None:
    tr = _Track(150).drive(-15, 30)
    queue_start = tr.t
    tr.stop(60)
    called_forward = tr.t
    tr.drive(0, 8).stop(90).drive(40, 15).stop(10)  # still in the zone: gives way at the exit
    tr.to_dump_and_back()

    c = _one_cycle(tr)

    assert c.queue_s == (called_forward - queue_start).total_seconds()


def test_segments_still_sum_to_the_cycle_time() -> None:
    tr = _Track(150).drive(-45, 30).stop(60).drive(-30, 8).stop(60).drive(-15, 8).stop(40)
    tr.drive(0, 8).stop(90).to_dump_and_back()

    c = _one_cycle(tr)

    total = c.queue_s + c.load_s + c.haul_s + c.dump_s + c.return_s
    assert abs(total - c.cycle_time_s) < 1e-6
