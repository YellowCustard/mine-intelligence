"""Gate access-event ingest — the simulator-first driver for access-control (FP-07).

Mirrors the "build against a simulator, not hardware" rule (brief §10): the whole access
path — normalise → record → authorise → alarm — is developed and tested here against
**recorded** gate events, so it is ready the moment the Dahua gate / iHUA tag / turnstile
interfaces are available (a Phase-0 unknown). The live poller is a thin driver added later; it
will call the same :func:`minemonitor.access.service.ingest_access_event`, and the normaliser
and its tests do not change.

The normaliser is the **no-biometrics boundary** (brief §4): a raw event carrying a face
*template* or *image* is **refused**, not stored. ``credential_ref`` is only ever an opaque
tag/card/face-*event* id, and identity is a soft reference to ``operators``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from minemonitor.access import service
from minemonitor.contracts import EventV1
from minemonitor.storage.models import AccessEvent

log = logging.getLogger("minemonitor.ingest.access")

# Keys that would carry biometric data. Their presence means the source is trying to send a
# template/image — refuse the whole event; that data stays in the vendor appliance (brief §4).
_BIOMETRIC_KEYS = frozenset(
    {"face_template", "template", "image", "photo", "face", "embedding", "biometric"}
)
_REQUIRED = ("id", "source_system", "gate_id", "time", "decision")


def _parse_ts(raw_time: Any) -> datetime:
    """Parse a gate event time to a timezone-aware datetime; naive values are rejected."""
    if not isinstance(raw_time, str):
        raise ValueError(f"access event 'time' must be an ISO-8601 string, got {type(raw_time)}")
    try:
        ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"access event 'time' is not ISO-8601: {raw_time!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"access event 'time' must be timezone-aware: {raw_time!r}")
    return ts


def normalise_access_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw gate event and return ingest kwargs. Pure; no I/O.

    Raises ``ValueError`` for a missing field, a biometric payload, or a non-timezone-aware
    timestamp — malformed or forbidden device data is rejected, not coerced.
    """
    biometric = _BIOMETRIC_KEYS & raw.keys()
    if biometric:
        raise ValueError(
            f"refusing access event carrying biometric data {sorted(biometric)}: "
            "templates/images stay in the vendor appliance (brief §4)"
        )
    for key in _REQUIRED:
        if raw.get(key) in (None, ""):
            raise ValueError(f"access event missing required field {key!r}")
    return {
        "source_system": str(raw["source_system"]),
        "source_event_id": str(raw["id"]),
        "gate_id": str(raw["gate_id"]),
        "decision": str(raw["decision"]),
        "ts": _parse_ts(raw["time"]),
        "credential_ref": raw.get("credential_ref"),
        "operator_ref": raw.get("operator_ref"),
        "reason": raw.get("reason"),
        "search_selected": bool(raw.get("search_selected", False)),
        "search_completed": raw.get("search_completed"),
    }


def ingest_access_events(
    session: Any,
    site_id: str,
    raw_events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[list[AccessEvent], list[EventV1]]:
    """Normalise, record and authorise a batch of raw gate events. Returns (new, alarms).

    Site-scoped and idempotent on the deterministic access id, so replays/backfill never
    double-record or double-alarm. A malformed or biometric event raises ``ValueError`` from
    the normaliser and aborts the batch before any commit.
    """
    new: list[AccessEvent] = []
    alarms: list[EventV1] = []
    for raw in raw_events:
        kwargs = normalise_access_event(raw)
        row, created, alarm = service.ingest_access_event(session, site_id, now=now, **kwargs)
        if created:
            new.append(row)
        if alarm is not None:
            alarms.append(alarm)
    if commit:
        session.commit()
    log.info(
        "access events ingested",
        extra={
            "site_id": site_id,
            "received": len(raw_events),
            "new": len(new),
            "alarms": len(alarms),
        },
    )
    return new, alarms


# A small recorded set — the fixture role the simulator plays for GNSS. Times are explicit
# Africa/Harare offsets (+02:00). No biometric field anywhere: credential_ref is opaque.
SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "id": "g-1001",
        "source_system": "dahua_gate",
        "gate_id": "main-gate",
        "time": "2026-09-12T06:02:11+02:00",
        "decision": "granted",
        "operator_ref": "OP-001",
        "credential_ref": "face-evt-5521",
        "search_selected": True,
        "search_completed": True,
    },
    {
        "id": "g-1002",
        "source_system": "ihua_tag",
        "gate_id": "visitor-gate",
        "time": "2026-09-12T06:05:40+02:00",
        "decision": "denied",
        "operator_ref": None,
        "credential_ref": "tag-VIS-77",
        "reason": "no active visitor booking",
    },
]


def main() -> None:
    """Replay the sample gate events into the configured database (a local demo)."""
    from minemonitor.config import get_settings
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory

    settings = get_settings()
    configure_logging(settings.log_level)
    factory = get_session_factory()
    with factory() as session:
        new, alarms = ingest_access_events(session, settings.default_site_id, SAMPLE_EVENTS)
    print(f"ingested {len(new)} access events, raised {len(alarms)} alarms (replay is idempotent)")


if __name__ == "__main__":
    main()
