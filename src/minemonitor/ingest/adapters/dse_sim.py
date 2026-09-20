"""DSE generator reading ingest — the simulator-first driver for FP-11.

Mirrors the "build against a simulator, not hardware" rule (brief §10): the whole generator
path — normalise → record (write-once) → derive fault/unexpected-run alarms → maintenance
health — is developed and tested here against **recorded** readings, so it is ready the moment
the DSE controller network is available (Modbus TCP register map — a Phase-0 unknown). The live
Modbus poller added then calls the same :func:`minemonitor.generator.service.record_reading`;
the normaliser and its tests do not change.

The vendor register→field mapping is the one genuine unknown, isolated to :func:`_to_raw` (the
``_to_raw`` seam) — confirmed against the real DSE model on site and changed there only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from minemonitor.contracts import EventV1
from minemonitor.generator import service
from minemonitor.storage.models import GeneratorReading

log = logging.getLogger("minemonitor.ingest.generator")

_REQUIRED = ("asset_id", "time", "running")


def _parse_ts(raw_time: Any) -> datetime:
    """Parse a controller time to a timezone-aware datetime; naive values are rejected."""
    if not isinstance(raw_time, str):
        raise ValueError(
            f"generator reading 'time' must be an ISO-8601 string, got {type(raw_time)}"
        )
    try:
        ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"generator reading 'time' is not ISO-8601: {raw_time!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"generator reading 'time' must be timezone-aware: {raw_time!r}")
    return ts


def _num_or_none(raw: Any, field: str) -> float | None:
    """Parse an optional numeric field; a present-but-non-numeric value is rejected."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"generator reading {field!r} is not numeric: {raw!r}") from exc


def normalise_reading(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw generator reading and return ingest kwargs. Pure; no I/O.

    Raises ``ValueError`` for a missing required field, a non-numeric measured value, or a
    non-timezone-aware timestamp — malformed controller data is rejected, not coerced.
    """
    for key in _REQUIRED:
        if raw.get(key) in (None, ""):
            raise ValueError(f"generator reading missing required field {key!r}")
    return {
        "asset_id": str(raw["asset_id"]),
        "ts": _parse_ts(raw["time"]),
        "running": bool(raw["running"]),
        "source": str(raw.get("source", "dse_sim")),
        "engine_hours": _num_or_none(raw.get("engine_hours"), "engine_hours"),
        "fuel_level_pct": _num_or_none(raw.get("fuel_level_pct"), "fuel_level_pct"),
        "expected_running": (
            bool(raw["expected_running"]) if raw.get("expected_running") is not None else None
        ),
        "fault": bool(raw.get("fault", False)),
        "fault_code": (str(raw["fault_code"]) if raw.get("fault_code") else None),
    }


def _to_raw(register: dict[str, Any], *, asset_id: str) -> dict[str, Any]:
    """Map one DSE controller register read to a raw reading — the vendor-format seam.

    Best-effort against a *plausible* DSE register layout; the real register map (per DSE model,
    e.g. DSE73xx/74xx/8xxx) is confirmed on site and changed **here only**.
    """
    return {
        "asset_id": asset_id,
        "time": register.get("timestamp") or register.get("ts") or "",
        "running": bool(register.get("running") or register.get("engine_running")),
        "engine_hours": register.get("engine_hours") or register.get("run_hours"),
        "fuel_level_pct": register.get("fuel_level") or register.get("fuel_pct"),
        "expected_running": register.get("expected_running"),
        "fault": bool(register.get("fault") or register.get("common_alarm")),
        "fault_code": register.get("fault_code") or register.get("alarm_code"),
        "source": "dse_modbus",
    }


def ingest_readings(
    session: Any,
    site_id: str,
    raw_readings: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[list[GeneratorReading], list[EventV1]]:
    """Normalise, record and alarm a batch of raw generator readings. Returns (new, alarms).

    Site-scoped and idempotent on the deterministic reading id, so replays/backfill never
    double-record or double-alarm. A malformed reading raises ``ValueError`` from the
    normaliser and aborts the batch before any commit.
    """
    new: list[GeneratorReading] = []
    alarms: list[EventV1] = []
    for raw in raw_readings:
        kwargs = normalise_reading(raw)
        row, created, evs = service.record_reading(session, site_id=site_id, now=now, **kwargs)
        if created:
            new.append(row)
        alarms.extend(evs)
    if commit:
        session.commit()
    log.info(
        "generator readings ingested",
        extra={
            "site_id": site_id,
            "received": len(raw_readings),
            "new": len(new),
            "alarms": len(alarms),
        },
    )
    return new, alarms


# A small recorded set — the fixture role the simulator plays. Times are explicit Africa/Harare
# offsets (+02:00). Plausible standby-genset readings, incl. one fault and one unexpected run.
SAMPLE_READINGS: list[dict[str, Any]] = [
    {
        "asset_id": "GEN-01",
        "time": "2026-09-12T06:00:00+02:00",
        "running": True,
        "engine_hours": 1240.5,
        "fuel_level_pct": 82.0,
        "expected_running": True,
        "fault": False,
        "source": "dse_sim",
    },
    {
        "asset_id": "GEN-02",
        "time": "2026-09-12T06:00:00+02:00",
        "running": True,
        "engine_hours": 980.0,
        "fuel_level_pct": 40.0,
        "expected_running": False,  # scheduled off but running → unexpected-run alarm
        "fault": False,
        "source": "dse_sim",
    },
    {
        "asset_id": "GEN-01",
        "time": "2026-09-12T07:00:00+02:00",
        "running": True,
        "engine_hours": 1241.5,
        "fuel_level_pct": 80.0,
        "expected_running": True,
        "fault": True,  # a fault → fault alarm
        "fault_code": "LOW_OIL_PRESSURE",
        "source": "dse_sim",
    },
]


def main() -> None:
    """Replay the sample generator readings into the configured database (a local demo)."""
    from minemonitor.config import get_settings
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory

    settings = get_settings()
    configure_logging(settings.log_level)
    factory = get_session_factory()
    with factory() as session:
        new, alarms = ingest_readings(session, settings.default_site_id, SAMPLE_READINGS)
    print(
        f"ingested {len(new)} generator readings, raised {len(alarms)} alarms "
        "(replay is idempotent)"
    )


if __name__ == "__main__":
    main()
