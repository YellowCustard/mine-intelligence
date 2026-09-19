"""Dahua NVR AI-event normaliser — raw NVR "smart" event → ``event.v1``.

The RAN Mines site runs a closed 118-camera Dahua estate whose NVR already produces AI
"smart" events (line-crossing, area intrusion, loitering) that currently go unused. This
turns one such raw event into a validated, advisory ``event.v1`` so it lands in the *same*
unified alarm queue as a GNSS geofence breach — the control room groups by severity, not by
which sensor saw it (brief §5).

This module is the **pure boundary**: a raw NVR event dict in, a validated ``EventV1`` out,
no I/O and no database — so it is fully testable without hardware or the vendor API. The
replay driver and the (future) live poller live in :mod:`dahua_nvr_sim`; they call this.

Two invariants are enforced *here*, at the boundary:

- **No identity, ever** (brief §4). Identity-bearing NVR event types (face
  recognition/capture) are refused, not ingested; and the event ``detail`` is built from a
  strict allow-list, so a name/face field in the raw payload can never leak into our store.
  Vision here is count/scene-level: "a person crossed the line", never "who".
- **Advisory only** (brief §15). Every event carries ``advisory: true``; nothing here
  actuates plant.

Malformed input is rejected loudly (``ValueError``), never silently coerced (brief §12).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from minemonitor.contracts import EventV1
from minemonitor.contracts.event import EventType, Severity
from minemonitor.contracts.vision import VisionVendorEventV1

# Dahua IVS "smart" event codes we map to operational alarm types. Each maps to an
# ``event.v1`` (type, severity). Kept small and explicit: an unmapped code is rejected, not
# guessed, so a new NVR capability is a deliberate addition here, not a silent passthrough.
_TYPE_MAP: dict[str, tuple[EventType, Severity]] = {
    "CrossLineDetection": ("zone_breach", "warning"),  # tripwire crossed
    "CrossRegionDetection": ("zone_breach", "critical"),  # intrusion into an area
    "IntrusionDetection": ("zone_breach", "critical"),
    "LoiteringDetection": ("zone_dwell", "warning"),
    "Loitering": ("zone_dwell", "warning"),
}

# Human-readable labels for the summary line (no identity — the object class, if any, is a
# generic "person"/"vehicle", carried in detail, never a name).
_LABEL: dict[str, str] = {
    "CrossLineDetection": "Tripwire crossing",
    "CrossRegionDetection": "Area intrusion",
    "IntrusionDetection": "Area intrusion",
    "LoiteringDetection": "Loitering",
    "Loitering": "Loitering",
}

# Platform-normalised type per vendor code (VISION_BUILD_GATE §8): the stable name the vendor
# event maps to, carried on the ``vision.vendor_event.v1`` record independently of the
# ``event.v1`` alarm type. Isolated here so a new vendor code is a deliberate addition.
_NORMALIZED_TYPE: dict[str, str] = {
    "CrossLineDetection": "nvr_line_crossing",
    "CrossRegionDetection": "nvr_intrusion",
    "IntrusionDetection": "nvr_intrusion",
    "LoiteringDetection": "nvr_loitering",
    "Loitering": "nvr_loitering",
}

# The vendor system these events come from (Stage A: the Dahua NVR).
SOURCE_SYSTEM = "dahua_nvr"

# Identity-bearing NVR event types. We refuse these outright: face templates/identities stay
# inside the vendor appliance on the mine's own network (brief §4). If a gate ever needs an
# access decision, that is a separate, consented access.granted/denied event — not this.
_IDENTITY_TYPES = frozenset({"FaceDetection", "FaceRecognition", "FaceCapture", "FaceComparison"})

# The only raw keys allowed into ``detail``. Anything else in the payload (notably any
# identity field) is dropped — the store cannot hold what the allow-list does not admit.
_DETAIL_KEYS = ("object", "confidence", "rule", "count")

# Required keys on every raw NVR event.
_REQUIRED = ("id", "type", "channel", "time")

SOURCE_PREFIX = "nvr"


def _parse_ts(raw_time: Any) -> datetime:
    """Parse an NVR event time to a timezone-aware UTC-comparable datetime.

    The timestamp must be timezone-aware: an NVR emits local time and a naive value would
    force a silent timezone assumption, which the boundary must not make (brief §12, §3). The
    live driver attaches the camera's zone; the fixtures use explicit offsets.
    """
    if not isinstance(raw_time, str):
        raise ValueError(f"NVR event 'time' must be an ISO-8601 string, got {type(raw_time)}")
    try:
        ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"NVR event 'time' is not ISO-8601: {raw_time!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"NVR event 'time' must be timezone-aware: {raw_time!r}")
    return ts


def normalise_nvr_event(
    raw: dict[str, Any],
    *,
    site_id: str,
    zone_id: str | None = None,
    camera_name: str | None = None,
) -> EventV1:
    """Turn one raw NVR AI event into a validated ``event.v1``. Pure; no I/O.

    ``zone_id`` and ``camera_name`` are resolved by the caller from the camera registry
    (a channel maps to a registered camera, which may map to an operational zone). The
    ``event_id`` is **deterministic** — derived from the site, channel and NVR event id — so
    replaying or backfilling the same event is idempotent (the same primary key, deduped
    upstream) and never double-alarms.

    Raises ``ValueError`` for a missing field, an unknown event code, an identity-bearing
    event, or a non-timezone-aware timestamp — malformed device data is rejected, not coerced.
    """
    for key in _REQUIRED:
        if raw.get(key) in (None, ""):
            raise ValueError(f"NVR event missing required field {key!r}")

    ev_type = str(raw["type"])
    if ev_type in _IDENTITY_TYPES:
        raise ValueError(
            f"refusing to ingest identity-bearing NVR event {ev_type!r}: "
            "biometric identity stays in the vendor appliance (brief §4)"
        )
    if ev_type not in _TYPE_MAP:
        raise ValueError(f"unknown NVR event type {ev_type!r}")

    mm_type, severity = _TYPE_MAP[ev_type]
    ts = _parse_ts(raw["time"])
    channel = raw["channel"]
    cam_ref = camera_name or f"ch{channel}"

    # Deterministic, idempotent primary key: same raw event → same event_id.
    event_id = f"nvr-{site_id}-{channel}-{raw['id']}"

    # PII-free detail from a strict allow-list — an identity field in ``raw`` cannot pass.
    # ``provenance`` marks this alarm as a vendor NVR inference, a distinct class from
    # first-party perception; ``vendor_confidence`` is the vendor's own score, carried
    # verbatim and never presented as a first-party confidence (VISION_BUILD_GATE §3.6).
    detail: dict[str, Any] = {
        "nvr_event_type": ev_type,
        "channel": channel,
        "provenance": "vendor_inferred",
    }
    for key in _DETAIL_KEYS:
        val = raw.get(key)
        if val is not None:
            detail[key] = val
    if raw.get("confidence") is not None:
        detail["vendor_confidence"] = raw["confidence"]

    # The alarm points back at its stored vendor record (same deterministic id) so an
    # operator can always trace the vendor-inferred origin.
    evidence: dict[str, Any] = {"nvr_event_id": str(raw["id"]), "vendor_event_id": event_id}
    if raw.get("clip"):
        evidence["clip_uri"] = raw["clip"]  # a reference only; the clip stays on the edge

    where = f" in {camera_name}" if camera_name else f" on channel {channel}"
    summary = f"{_LABEL[ev_type]}{where}"

    return EventV1(
        schema="event.v1",
        event_id=event_id,
        site_id=site_id,
        ts=ts,
        type=mm_type,
        severity=severity,
        asset_id=None,
        zone_id=zone_id,
        source=f"{SOURCE_PREFIX}:{cam_ref}",
        summary=summary,
        detail=detail,
        evidence=evidence,
        advisory=True,
        state="open",
    )


def build_vendor_event(
    raw: dict[str, Any],
    *,
    site_id: str,
    camera_id: str | None = None,
) -> VisionVendorEventV1:
    """Build the ``vision.vendor_event.v1`` record for a raw NVR event. Pure; no I/O.

    The normalised, stored vendor record (VISION_BUILD_GATE §4.5) — **vendor-inferred**
    provenance, the vendor's ``confidence`` carried verbatim as ``vendor_confidence`` (never a
    first-party confidence). Shares the deterministic id with the promoted ``event.v1`` so the
    two cross-link. Applies the same required-field / identity / unknown-type validation as
    :func:`normalise_nvr_event`, so an identity-bearing or malformed event is refused here too.

    ``camera_id`` is the registered camera id resolved by the caller, or a
    ``dahua_nvr:<channel>`` placeholder when the channel is unmapped — never dropped.
    """
    for key in _REQUIRED:
        if raw.get(key) in (None, ""):
            raise ValueError(f"NVR event missing required field {key!r}")
    ev_type = str(raw["type"])
    if ev_type in _IDENTITY_TYPES:
        raise ValueError(
            f"refusing to ingest identity-bearing NVR event {ev_type!r}: "
            "biometric identity stays in the vendor appliance (brief §4)"
        )
    if ev_type not in _NORMALIZED_TYPE:
        raise ValueError(f"unknown NVR event type {ev_type!r}")
    channel = str(raw["channel"])
    conf = raw.get("confidence")
    return VisionVendorEventV1(
        schema="vision.vendor_event.v1",
        vendor_event_id=f"nvr-{site_id}-{channel}-{raw['id']}",
        site_id=site_id,
        source_system=SOURCE_SYSTEM,
        source_event_id=str(raw["id"]),
        camera_id=camera_id or f"{SOURCE_SYSTEM}:{channel}",
        channel=channel,
        vendor_type=ev_type,
        normalized_type=_NORMALIZED_TYPE[ev_type],
        vendor_confidence=float(conf) if conf is not None else None,
        vendor_rule_name=(str(raw["rule"]) if raw.get("rule") else None),
        ts=_parse_ts(raw["time"]),
        clip_ref=(str(raw["clip"]) if raw.get("clip") else None),
        provenance="vendor_inferred",
        advisory=True,
    )
