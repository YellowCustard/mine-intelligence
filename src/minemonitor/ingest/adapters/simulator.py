"""Synthetic movement generator — the first adapter, and the fixture set.

Replays a plausible shift for ~9 machines: haul trucks looping the pit face → ROM
pad, a shared loader at the face so genuine **queue time** emerges (the commercial
metric M4 measures), an excavator working the face, two patrol vehicles on the
haul road, and a light vehicle that wanders into the restricted magazine (which
M3's restricted-entry rule must catch).

Everything it emits is a ``PositionIngest`` — identical at the ingest boundary to
a real tracker (brief §10). Coordinates are plausible PLACEHOLDERS near the
placeholder site, pending the real survey (brief §14).

Run it against a broker:
    python -m minemonitor.ingest.adapters.simulator
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from minemonitor.ingest.adapters.base import DeviceAdapter
from minemonitor.ingest.geo import bearing_deg, haversine_m, offset_m
from minemonitor.ingest.service import PositionIngest

SOURCE = "simulator"

# Placeholder anchor near the placeholder site — NOT a survey (brief §14).
_BASE_LAT, _BASE_LON = -17.8252, 31.0335


def _pt(north_m: float, east_m: float) -> tuple[float, float]:
    return offset_m(_BASE_LAT, _BASE_LON, north_m, east_m)


# Named locations (metres north/east of the base anchor).
FACE = _pt(0, 0)  # pit face — load point
ROM = _pt(600, 200)  # ROM pad / crusher — ore dump
WASTE = _pt(-400, 800)  # waste dump
MAGAZINE = _pt(200, -500)  # restricted: explosives magazine
WORKSHOP = _pt(-600, -200)  # workshop / muster


def _move_towards(
    lat: float, lon: float, target: tuple[float, float], speed_mps: float, dt_s: float
) -> tuple[float, float, float, bool]:
    """Step from (lat, lon) toward target. Returns (lat, lon, heading, arrived)."""
    dist = haversine_m(lat, lon, target[0], target[1])
    heading = bearing_deg(lat, lon, target[0], target[1])
    travel = speed_mps * dt_s
    if travel >= dist or dist < 1e-6:
        return target[0], target[1], heading, True
    frac = travel / dist
    return lat + (target[0] - lat) * frac, lon + (target[1] - lon) * frac, heading, False


@dataclass
class _Asset:
    asset_id: str
    asset_class: str
    lat: float
    lon: float
    speed_kph: float  # cruising speed
    state: str
    ignition: bool = True
    heading: float = 0.0
    dwell_left_s: float = 0.0
    queue_since: datetime | None = None
    queue_total_s: float = 0.0  # ground truth for M4 validation
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    wp_idx: int = 0


class Simulator(DeviceAdapter):
    """Steps a fleet forward at a fixed tick and emits positions each tick."""

    source = SOURCE

    def __init__(
        self,
        site_id: str = "kn-zw-01",
        *,
        seed: int = 1,
        start: datetime | None = None,
        tick_s: float = 1.0,
        load_s: float = 90.0,
        dump_s: float = 40.0,
        jitter_m: float = 2.5,
    ) -> None:
        self.site_id = site_id
        self.tick_s = tick_s
        self.load_s = load_s
        self.dump_s = dump_s
        self.jitter_m = jitter_m
        self._rng = random.Random(seed)
        self.now = start or datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)

        # Shared loader at the face: FIFO of trucks waiting; one loads at a time.
        self._load_queue: list[str] = []
        self._loading: str | None = None
        self._load_finish: datetime | None = None

        self.assets: dict[str, _Asset] = {}
        self._build_fleet()

    # -- fleet construction -------------------------------------------------

    def _build_fleet(self) -> None:
        # 5 haul trucks, staggered around the cycle so a queue forms at the face.
        stagger = ["RETURNING", "HAULING", "RETURNING", "LOADING_APPROACH", "HAULING"]
        for i, phase in enumerate(stagger, start=101):
            start_pt = FACE if phase.startswith("RETURN") or "LOAD" in phase else ROM
            self.assets[f"HT-{i}"] = _Asset(
                asset_id=f"HT-{i}",
                asset_class="haul_truck",
                lat=start_pt[0] + self._jit(),
                lon=start_pt[1] + self._jit(),
                speed_kph=28.0,
                state="RETURNING_EMPTY" if start_pt is not FACE else "QUEUING",
            )
        # Trucks that begin at the face queue immediately.
        for a in self.assets.values():
            if a.state == "QUEUING":
                self._load_queue.append(a.asset_id)

        # Excavator working the face — small local shuffles.
        self.assets["EX-01"] = _Asset(
            asset_id="EX-01",
            asset_class="excavator",
            lat=FACE[0] + self._jit(6),
            lon=FACE[1] + self._jit(6),
            speed_kph=3.0,
            state="WORKING",
        )
        # Two patrol machines on the haul road.
        self.assets["WB-01"] = _Asset(
            asset_id="WB-01",
            asset_class="water_bowser",
            lat=ROM[0],
            lon=ROM[1],
            speed_kph=18.0,
            state="PATROL",
            waypoints=[FACE, ROM, WASTE, ROM],
        )
        self.assets["GR-01"] = _Asset(
            asset_id="GR-01",
            asset_class="grader",
            lat=WASTE[0],
            lon=WASTE[1],
            speed_kph=10.0,
            state="PATROL",
            waypoints=[WASTE, ROM, FACE, WORKSHOP],
        )
        # Light vehicle: workshop → (through) magazine → workshop. Enters the
        # restricted zone deliberately so M3 has a breach to catch.
        self.assets["LV-07"] = _Asset(
            asset_id="LV-07",
            asset_class="light_vehicle",
            lat=WORKSHOP[0],
            lon=WORKSHOP[1],
            speed_kph=35.0,
            state="PATROL",
            waypoints=[WORKSHOP, MAGAZINE, ROM, WORKSHOP],
        )

    # -- stepping -----------------------------------------------------------

    def _jit(self, scale: float | None = None) -> float:
        """A small metric jitter expressed as a degree offset (rough)."""
        m = self._rng.gauss(0, scale if scale is not None else self.jitter_m)
        return m / 111_320.0

    def _speed_mps(self, kph: float) -> float:
        return kph / 3.6

    def step(self) -> list[PositionIngest]:
        """Advance every asset by one tick and return their positions."""
        self._service_loader()
        out: list[PositionIngest] = []
        for a in self.assets.values():
            if a.asset_class == "haul_truck":
                self._step_truck(a)
            elif a.asset_class == "excavator":
                self._step_excavator(a)
            else:
                self._step_patrol(a)
            out.append(self._emit(a))
        self.now += timedelta(seconds=self.tick_s)
        return out

    def _service_loader(self) -> None:
        if (
            self._loading is not None
            and self._load_finish is not None
            and self.now >= self._load_finish
        ):
            self._loading = None
            self._load_finish = None
        if self._loading is None and self._load_queue:
            self._loading = self._load_queue.pop(0)
            self._load_finish = self.now + timedelta(seconds=self.load_s)

    def _step_truck(self, a: _Asset) -> None:
        if a.state == "RETURNING_EMPTY":
            a.ignition = True
            a.lat, a.lon, a.heading, arrived = _move_towards(
                a.lat, a.lon, FACE, self._speed_mps(a.speed_kph), self.tick_s
            )
            if arrived:
                a.state = "QUEUING"
                a.queue_since = self.now
                self._load_queue.append(a.asset_id)
        elif a.state == "QUEUING":
            a.ignition = True  # stationary, engine on — this is queue time
            if self._loading == a.asset_id:
                if a.queue_since is not None:
                    a.queue_total_s += (self.now - a.queue_since).total_seconds()
                    a.queue_since = None
                a.state = "LOADING"
        elif a.state == "LOADING":
            a.ignition = True
            if self._loading != a.asset_id:  # loader finished with us
                a.state = "HAULING_LOADED"
        elif a.state == "HAULING_LOADED":
            a.ignition = True
            a.lat, a.lon, a.heading, arrived = _move_towards(
                a.lat, a.lon, ROM, self._speed_mps(a.speed_kph), self.tick_s
            )
            if arrived:
                a.state = "DUMPING"
                a.dwell_left_s = self.dump_s
        elif a.state == "DUMPING":
            a.ignition = True
            a.dwell_left_s -= self.tick_s
            if a.dwell_left_s <= 0:
                a.state = "RETURNING_EMPTY"

    def _step_excavator(self, a: _Asset) -> None:
        # Small shuffles around the face; occasionally idle with engine on.
        a.ignition = True
        a.lat = FACE[0] + self._jit(8)
        a.lon = FACE[1] + self._jit(8)
        a.heading = self._rng.uniform(0, 360)

    def _step_patrol(self, a: _Asset) -> None:
        a.ignition = True
        target = a.waypoints[a.wp_idx]
        a.lat, a.lon, a.heading, arrived = _move_towards(
            a.lat, a.lon, target, self._speed_mps(a.speed_kph), self.tick_s
        )
        if arrived:
            a.wp_idx = (a.wp_idx + 1) % len(a.waypoints)

    def _emit(self, a: _Asset) -> PositionIngest:
        moving = a.state in {"RETURNING_EMPTY", "HAULING_LOADED", "PATROL", "WORKING"}
        speed = a.speed_kph if a.state in {"RETURNING_EMPTY", "HAULING_LOADED", "PATROL"} else 0.0
        if a.asset_class == "excavator":
            speed = self._rng.uniform(0, 3)
        return PositionIngest(
            schema="asset.position.v1",
            site_id=self.site_id,
            asset_id=a.asset_id,
            ts=self.now,
            lat=round(a.lat + self._jit(), 6),
            lon=round(a.lon + self._jit(), 6),
            speed_kph=round(max(0.0, speed + (self._rng.gauss(0, 0.5) if moving else 0.0)), 1),
            heading_deg=round(a.heading, 0),
            hdop=round(self._rng.uniform(0.6, 1.4), 1),
            satellites=self._rng.randint(8, 12),
            ignition=a.ignition,
            source=self.source,
        )

    # -- adapter interface --------------------------------------------------

    def positions(self, ticks: int | None = None) -> Iterator[PositionIngest]:
        """Yield positions. ``ticks=None`` runs unbounded (live use)."""
        emitted = 0
        while ticks is None or emitted < ticks:
            yield from self.step()
            emitted += 1


def main() -> None:
    """Run the simulator live, publishing to MQTT at real-time 1 Hz."""
    import time

    from minemonitor.config import get_settings
    from minemonitor.ingest.mqtt import MqttPublisher
    from minemonitor.logging_config import configure_logging

    configure_logging(get_settings().log_level)
    sim = Simulator()
    publisher = MqttPublisher()
    publisher.start()
    try:
        while True:
            for payload in sim.step():
                publisher.publish_position(payload)
            time.sleep(sim.tick_s)
    except KeyboardInterrupt:
        pass
    finally:
        publisher.stop()


if __name__ == "__main__":
    main()
