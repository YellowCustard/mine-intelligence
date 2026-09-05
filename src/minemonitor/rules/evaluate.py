"""Turn confirmed zone transitions and in-zone fixes into ``event.v1`` records.

Each rule fires **once per episode**, not once per fix: a restricted entry fires
on the confirmed entry transition; overspeed fires once per sustained-over spell;
dwell fires once per stationary spell. That is what makes one real breach produce
one alarm, not a storm (brief §9, M3 acceptance).
"""

from __future__ import annotations

from minemonitor.contracts import EventV1
from minemonitor.contracts.position import AssetPositionV1
from minemonitor.events.repository import new_event_id
from minemonitor.rules.config import STATIONARY_KPH, RuleConfig
from minemonitor.storage.models import AssetZoneState, Zone

SOURCE = "gnss_geofence"


def _event(
    *, pos: AssetPositionV1, type_: str, severity: str, zone: Zone, summary: str, detail: dict
) -> EventV1:
    return EventV1(
        schema="event.v1",
        event_id=new_event_id(),
        site_id=pos.site_id,
        ts=pos.ts,
        type=type_,
        severity=severity,
        asset_id=pos.asset_id,
        zone_id=zone.zone_id,
        source=SOURCE,
        summary=summary,
        detail=detail,
        advisory=True,
        state="open",
    )


def on_entry(
    *, pos: AssetPositionV1, zone: Zone, cfg: RuleConfig, asset_class: str
) -> EventV1 | None:
    """Restricted-zone rule: entry by an unauthorised asset class is a breach."""
    if zone.kind != "restricted":
        return None
    authorized = cfg.authorized_classes or ()
    if asset_class in authorized:
        return None
    return _event(
        pos=pos,
        type_="zone_breach",
        severity=cfg.breach_severity,
        zone=zone,
        summary=f"{pos.asset_id} entered {zone.name} without authorisation",
        detail={"asset_class": asset_class, "speed_kph": pos.speed_kph},
    )


def on_inzone_fix(
    *, pos: AssetPositionV1, zone: Zone, cfg: RuleConfig, state: AssetZoneState
) -> list[EventV1]:
    """Overspeed and dwell rules for a fix while confirmed inside the zone.

    Mutates the per-episode counters on ``state``.
    """
    events: list[EventV1] = []
    speed = pos.speed_kph or 0.0

    if cfg.speed_limit_kph is not None:
        if speed > cfg.speed_limit_kph:
            state.overspeed_consec += 1
            if state.overspeed_consec >= cfg.overspeed_consecutive and not state.overspeed_fired:
                state.overspeed_fired = True
                events.append(
                    _event(
                        pos=pos,
                        type_="overspeed",
                        severity=cfg.overspeed_severity,
                        zone=zone,
                        summary=(
                            f"{pos.asset_id} over {cfg.speed_limit_kph:g} kph in "
                            f"{zone.name} ({speed:g} kph)"
                        ),
                        detail={
                            "speed_kph": speed,
                            "limit_kph": cfg.speed_limit_kph,
                        },
                    )
                )
        else:
            state.overspeed_consec = 0
            state.overspeed_fired = False

    if cfg.dwell_s is not None:
        if speed < STATIONARY_KPH:
            if state.stationary_since is None:
                state.stationary_since = pos.ts
            elif (
                not state.dwell_fired
                and (pos.ts - state.stationary_since).total_seconds() >= cfg.dwell_s
            ):
                state.dwell_fired = True
                dwell_s = (pos.ts - state.stationary_since).total_seconds()
                events.append(
                    _event(
                        pos=pos,
                        type_="zone_dwell",
                        severity=cfg.dwell_severity,
                        zone=zone,
                        summary=(f"{pos.asset_id} stationary in {zone.name} for {int(dwell_s)}s"),
                        detail={"dwell_s": dwell_s, "threshold_s": cfg.dwell_s},
                    )
                )
        else:
            state.stationary_since = None
            state.dwell_fired = False

    return events
