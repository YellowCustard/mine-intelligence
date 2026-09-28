"""Temporal rule engine: observations over time → evidenced operational events. Pure Python.

The operational layer of ``docs/VISION_BUILD_GATE.md`` §3.3/§7. A detector answers *"what is in
the frame?"*; this engine answers *"what does that mean on this site?"* — and **never from one
frame**. It is the vision analogue of the GNSS zone debounce:

- **Confirm** a condition only after ``min_observations`` *consecutive* qualifying observations;
  a boundary-hugging track that flickers in/out never builds a streak, so it never alarms.
- **Dwell** is measured from the start of the confirming streak; the event fires once the condition
  has held for ``min_dwell_ms``.
- **Release** only after ``hysteresis_observations`` consecutive non-qualifying observations, so a
  single jittered box after confirmation neither re-arms nor clears the condition.
- **Cooldown** per dedupe key suppresses re-alarm churn on the same subject.
- A track that is simply *not seen* (occlusion) changes nothing; its state expires after
  ``window_ms`` without an observation.

Every event names its rule + version and carries evidence (window, tracks, frames, signals), so it
is explainable and **recomputable**: the engine consumes ``vision.observation.v1`` directly, and the
core replays stored observations through the same code when a rule improves. Event ids are
deterministic (and include the rule version), so a replay under the same version is idempotent and
a replay under a new version produces new events beside the old.

Rules are data (``vision.rule_version.v1``). Severity is not decided here — it is rule/zone policy,
applied when the core promotes an event to ``event.v1``.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from minemonitor.contracts.vision import (
    VisionObservationV1,
    VisionOperationalEventV1,
    VisionRuleVersionV1,
)

PRESENCE_TYPES = frozenset(
    {"person_in_restricted_zone", "vehicle_in_restricted_zone", "person_dwell"}
)
BAD_CAMERA_STATES = frozenset({"obstructed", "dark", "defocused", "no_signal"})
_MAX_EVIDENCE_FRAMES = 20
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class _Streak:
    streak_in: int = 0
    start: datetime | None = None
    confirmed: bool = False
    since: datetime | None = None
    streak_out: int = 0
    fired: bool = False
    last_seen: datetime | None = None
    hits: int = 0
    min_conf: float = 1.0
    frames: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_EVIDENCE_FRAMES))
    tracks: set[str] = field(default_factory=set)
    peak: int = 0

    def reset(self) -> None:
        self.streak_in = 0
        self.start = None
        self.confirmed = False
        self.since = None
        self.streak_out = 0
        self.fired = False
        self.hits = 0
        self.min_conf = 1.0
        self.frames.clear()
        self.tracks.clear()
        self.peak = 0


def _advance(st: _Streak, hit: bool, ts: datetime, rule: VisionRuleVersionV1) -> bool:
    """One qualifying/non-qualifying observation. True when the event should fire now."""
    p = rule.parameters
    st.last_seen = ts
    if hit:
        st.streak_out = 0
        if not st.confirmed:
            if st.streak_in == 0:
                st.start = ts
            st.streak_in += 1
            if st.streak_in >= p.min_observations:
                st.confirmed = True
                st.since = st.start
        st.hits += 1
        if st.confirmed and not st.fired and st.since is not None:
            return (ts - st.since) >= timedelta(milliseconds=p.min_dwell_ms)
        return False
    if not st.confirmed:
        st.reset()
        st.last_seen = ts
        return False
    st.streak_out += 1
    if st.streak_out >= max(1, p.hysteresis_observations):
        st.reset()
        st.last_seen = ts
    return False


class TemporalEngine:
    """Per-camera rule state. Feed observations in time order; collect operational events."""

    def __init__(
        self,
        *,
        site_id: str,
        node_id: str,
        camera_id: str,
        rules: list[VisionRuleVersionV1],
        zones: dict[str, dict[str, Any]],
        zone_versions: dict[str, str] | None = None,
    ) -> None:
        self.site_id = site_id
        self.node_id = node_id
        self.camera_id = camera_id
        self.rules = rules
        self.zones = zones
        self.zone_versions = zone_versions or {}
        self._state: dict[tuple[str, ...], _Streak] = {}
        self._last_fired: dict[tuple[str, ...], datetime] = {}

    # -- public ----------------------------------------------------------------------------

    def step(
        self, obs: VisionObservationV1, *, camera_state: str | None = None
    ) -> list[VisionOperationalEventV1]:
        """Advance every rule by one observation (and optional camera-quality assessment)."""
        if obs.camera_id != self.camera_id:
            raise ValueError(f"observation for {obs.camera_id!r} fed to {self.camera_id!r} engine")
        out: list[VisionOperationalEventV1] = []
        for rule in self.rules:
            if rule.event_type in PRESENCE_TYPES:
                out.extend(self._presence(rule, obs))
            elif rule.event_type == "zone_occupancy_exceeded":
                out.extend(self._occupancy(rule, obs))
            elif rule.event_type == "camera_obstructed" and camera_state is not None:
                out.extend(self._camera(rule, obs, camera_state))
        self._expire(obs.ts)
        return out

    # -- rules -----------------------------------------------------------------------------

    def _zone_ok(self, rule: VisionRuleVersionV1, zone_id: str) -> bool:
        zone = self.zones.get(zone_id)
        if zone is None:
            return False
        kinds = rule.parameters.zone_kinds
        return not kinds or zone.get("kind") in kinds

    def _presence(
        self, rule: VisionRuleVersionV1, obs: VisionObservationV1
    ) -> list[VisionOperationalEventV1]:
        p = rule.parameters
        events = []
        in_frame: dict[str, set[str]] = {}
        conf: dict[str, float] = {}
        for o in obs.objects:
            if o.class_ not in p.classes or o.confidence < p.confidence_threshold:
                continue
            in_frame[o.track_id] = {z for z in o.zone_ids if self._zone_ok(rule, z)}
            conf[o.track_id] = o.confidence
        # hits: tracks seen inside qualifying zones
        for track_id, zone_ids in in_frame.items():
            for zone_id in zone_ids:
                key = ("presence", rule.rule_id, track_id, zone_id)
                st = self._state.setdefault(key, _Streak())
                fire = _advance(st, True, obs.ts, rule)
                self._note(st, obs.frame_id, track_id, conf[track_id])
                if fire:
                    ev = self._fire(rule, st, obs, key, zone_id=zone_id, track_id=track_id)
                    if ev is not None:
                        events.append(ev)
        # misses: tracks seen this frame but no longer in a zone they were tracked in
        for skey, sst in list(self._state.items()):
            if skey[0] != "presence" or skey[1] != rule.rule_id:
                continue
            if skey[2] in in_frame and skey[3] not in in_frame[skey[2]]:
                _advance(sst, False, obs.ts, rule)
        return events

    def _occupancy(
        self, rule: VisionRuleVersionV1, obs: VisionObservationV1
    ) -> list[VisionOperationalEventV1]:
        p = rule.parameters
        events = []
        for zone_id, zone in self.zones.items():
            cap = zone.get("max_occupancy")
            if not isinstance(cap, int) or cap < 0 or not self._zone_ok(rule, zone_id):
                continue
            members = [
                o
                for o in obs.objects
                if o.class_ in p.classes
                and o.confidence >= p.confidence_threshold
                and zone_id in o.zone_ids
            ]
            key = ("occupancy", rule.rule_id, zone_id)
            st = self._state.setdefault(key, _Streak())
            over = len(members) > cap
            fire = _advance(st, over, obs.ts, rule)
            if over:
                st.peak = max(st.peak, len(members))
                for o in members:
                    self._note(st, obs.frame_id, o.track_id, o.confidence)
            if fire:
                ev = self._fire(
                    rule,
                    st,
                    obs,
                    key,
                    zone_id=zone_id,
                    track_id=None,
                    extra={"count": st.peak, "max_occupancy": cap},
                )
                if ev is not None:
                    events.append(ev)
        return events

    def _camera(
        self, rule: VisionRuleVersionV1, obs: VisionObservationV1, camera_state: str
    ) -> list[VisionOperationalEventV1]:
        key = ("camera", rule.rule_id)
        st = self._state.setdefault(key, _Streak())
        bad = camera_state in BAD_CAMERA_STATES
        fire = _advance(st, bad, obs.ts, rule)
        if bad:
            self._note(st, obs.frame_id, None, 1.0)
        if not fire:
            return []
        ev = self._fire(
            rule, st, obs, key, zone_id=None, track_id=None, extra={"state": camera_state}
        )
        return [ev] if ev is not None else []

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _note(st: _Streak, frame_id: str, track_id: str | None, confidence: float) -> None:
        if st.confirmed or st.streak_in > 0:
            if not st.frames or st.frames[-1] != frame_id:
                st.frames.append(frame_id)
            if track_id is not None:
                st.tracks.add(track_id)
            st.min_conf = min(st.min_conf, confidence)

    def _fire(
        self,
        rule: VisionRuleVersionV1,
        st: _Streak,
        obs: VisionObservationV1,
        key: tuple[str, ...],
        *,
        zone_id: str | None,
        track_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> VisionOperationalEventV1 | None:
        st.fired = True  # one conclusion per confirmed presence, fired or suppressed
        assert st.since is not None
        last = self._last_fired.get(key)
        cooldown = timedelta(milliseconds=rule.parameters.cooldown_ms)
        if last is not None and obs.ts - last < cooldown:
            return None
        self._last_fired[key] = obs.ts
        dwell_ms = int((obs.ts - st.since).total_seconds() * 1000)
        subject_part = track_id.split(":", 1)[1] if track_id else (zone_id or "view")
        event_id = _SAFE.sub(
            "-",
            f"vop-{self.camera_id}-{rule.rule_id}-{rule.rule_version}-{subject_part}"
            f"-{st.since.strftime('%Y%m%dT%H%M%S%fZ')}",
        )
        tracks = sorted(st.tracks | ({track_id} if track_id else set()))
        signals: dict[str, Any] = {
            "dwell_ms": dwell_ms,
            "observations": st.hits,
            "min_conf": round(st.min_conf, 4),
        }
        if extra:
            signals.update(extra)
        confidence = round(min(1.0, max(0.0, st.min_conf)), 4)
        return VisionOperationalEventV1.model_validate(
            {
                "schema": "vision.operational_event.v1",
                "event_id": event_id,
                "site_id": self.site_id,
                "node_id": self.node_id,
                "camera_id": self.camera_id,
                "ts": obs.ts,
                "type": rule.event_type,
                "provenance": "inferred",
                "confidence": confidence,
                "rule_id": rule.rule_id,
                "rule_version": rule.rule_version,
                "subject": {"track_id": track_id} if track_id else None,
                "zone_id": zone_id,
                "zone_version": self.zone_versions.get(zone_id) if zone_id else None,
                "evidence": {
                    "observation_window": [st.since, obs.ts],
                    "track_ids": tracks,
                    "frame_ids": list(st.frames),
                    "signals": signals,
                    "clip_ref": None,
                },
                "advisory": True,
            }
        )

    def _expire(self, now: datetime) -> None:
        """Drop state for subjects unseen for longer than their rule's window (occlusion ended)."""
        windows = {
            r.rule_id: timedelta(milliseconds=max(r.parameters.window_ms, 1)) for r in self.rules
        }
        for key, st in list(self._state.items()):
            window = windows.get(key[1])
            if window is None or st.last_seen is None:
                continue
            if key[0] == "presence" and now - st.last_seen > window:
                del self._state[key]


def replay(
    observations: list[VisionObservationV1],
    *,
    rules: list[VisionRuleVersionV1],
    zones: dict[str, dict[str, Any]],
    node_id: str | None = None,
    zone_versions: dict[str, str] | None = None,
) -> list[VisionOperationalEventV1]:
    """Recompute operational events from stored observations (one camera, time-ordered)."""
    if not observations:
        return []
    ordered = sorted(observations, key=lambda o: (o.ts, o.frame_id))
    first = ordered[0]
    engine = TemporalEngine(
        site_id=first.site_id,
        node_id=node_id or first.node_id,
        camera_id=first.camera_id,
        rules=rules,
        zones=zones,
        zone_versions=zone_versions,
    )
    events: list[VisionOperationalEventV1] = []
    for obs in ordered:
        events.extend(engine.step(obs))
    return events
