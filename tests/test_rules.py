"""Unit tests for overspeed and dwell rule evaluation (once per episode)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minemonitor.contracts.position import AssetPositionV1
from minemonitor.rules.config import parse_rules
from minemonitor.rules.evaluate import on_inzone_fix
from minemonitor.storage.models import AssetZoneState, Zone


def _zone(kind: str, rules: dict) -> Zone:
    return Zone(zone_id="z", site_id="s", name="Z", kind=kind, geometry={}, rules=rules)


def _state() -> AssetZoneState:
    return AssetZoneState(
        site_id="s",
        asset_id="a",
        zone_id="z",
        inside=True,
        consec_in=2,
        consec_out=0,
        overspeed_consec=0,
        overspeed_fired=False,
        dwell_fired=False,
    )


def _pos(i: int, speed: float) -> AssetPositionV1:
    ts = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC) + timedelta(seconds=i)
    return AssetPositionV1(
        schema="asset.position.v1",
        site_id="s",
        asset_id="a",
        ts=ts,
        received_at=ts,
        lat=-17.8,
        lon=31.0,
        speed_kph=speed,
        ignition=True,
        source="test",
    )


def test_overspeed_fires_once_per_episode() -> None:
    zone = _zone("speed_limited", {"speed_limit_kph": 25, "overspeed_consecutive": 3})
    cfg = parse_rules(zone.kind, zone.rules)
    st = _state()
    events = []
    for i in range(6):  # six consecutive over-limit fixes
        events += on_inzone_fix(pos=_pos(i, 40), zone=zone, cfg=cfg, state=st)
    assert len(events) == 1  # one alarm for the sustained spell
    assert events[0].type == "overspeed"


def test_overspeed_needs_consecutive_fixes() -> None:
    zone = _zone("speed_limited", {"speed_limit_kph": 25, "overspeed_consecutive": 3})
    cfg = parse_rules(zone.kind, zone.rules)
    st = _state()
    events = []
    speeds = [40, 40, 10, 40, 40]  # never 3 in a row
    for i, spd in enumerate(speeds):
        events += on_inzone_fix(pos=_pos(i, spd), zone=zone, cfg=cfg, state=st)
    assert events == []


def test_overspeed_can_fire_again_after_dropping_under() -> None:
    zone = _zone("speed_limited", {"speed_limit_kph": 25, "overspeed_consecutive": 2})
    cfg = parse_rules(zone.kind, zone.rules)
    st = _state()
    speeds = [40, 40, 5, 40, 40]  # over, reset, over again
    events = []
    for i, spd in enumerate(speeds):
        events += on_inzone_fix(pos=_pos(i, spd), zone=zone, cfg=cfg, state=st)
    assert len(events) == 2


def test_dwell_fires_once_after_threshold() -> None:
    zone = _zone("loading", {"dwell_s": 5})
    cfg = parse_rules(zone.kind, zone.rules)
    st = _state()
    events = []
    for i in range(10):  # stationary for 10 s, threshold 5 s
        events += on_inzone_fix(pos=_pos(i, 0.0), zone=zone, cfg=cfg, state=st)
    assert len(events) == 1
    assert events[0].type == "zone_dwell"


def test_dwell_resets_when_moving() -> None:
    zone = _zone("loading", {"dwell_s": 5})
    cfg = parse_rules(zone.kind, zone.rules)
    st = _state()
    events = []
    for i in range(4):  # stationary but below threshold
        events += on_inzone_fix(pos=_pos(i, 0.0), zone=zone, cfg=cfg, state=st)
    events += on_inzone_fix(pos=_pos(4, 20.0), zone=zone, cfg=cfg, state=st)  # moves
    assert events == []
    assert st.stationary_since is None
