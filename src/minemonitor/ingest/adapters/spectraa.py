"""Laboratory assay ingest — the simulator-first driver for FP-08.

Mirrors the "build against a simulator, not hardware" rule (brief §10): the whole lab path —
parse export → normalise → record (write-once + hash) → detect anomalies → alarm — is developed
and tested here against **recorded** SpectrAA output, so it is ready the moment the real Agilent
2000-series AA export path (a SpectrAA result file / watch-folder — a Phase-0 unknown) is
confirmed on site. The live watch-folder driver added then calls the same
:func:`minemonitor.laboratory.service.ingest_result`; the normaliser and its tests do not change.

The vendor's exact column names are the one genuine unknown, isolated to :func:`_row_to_raw`
(the ``_to_raw`` seam) — confirming it against a real export flips this from recorded fixtures
to live with no other change. ``sample_ref`` is only ever an opaque lab label, never personal
data.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any

from minemonitor.contracts import EventV1
from minemonitor.laboratory import service
from minemonitor.storage.models import LaboratoryResult

log = logging.getLogger("minemonitor.ingest.laboratory")

_REQUIRED = ("instrument", "sample_ref", "element", "value", "unit", "time")


def _parse_ts(raw_time: Any) -> datetime:
    """Parse an instrument time to a timezone-aware datetime; naive values are rejected."""
    if not isinstance(raw_time, str):
        raise ValueError(f"lab result 'time' must be an ISO-8601 string, got {type(raw_time)}")
    try:
        ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"lab result 'time' is not ISO-8601: {raw_time!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"lab result 'time' must be timezone-aware: {raw_time!r}")
    return ts


def _parse_value(raw_value: Any) -> float:
    """Parse a measured concentration; a non-numeric value is rejected, never coerced to 0."""
    try:
        return float(raw_value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"lab result 'value' is not numeric: {raw_value!r}") from exc


def normalise_lab_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw lab record and return ingest kwargs. Pure; no I/O.

    Raises ``ValueError`` for a missing required field, a non-numeric value or a
    non-timezone-aware timestamp — malformed instrument data is rejected loudly, not coerced.
    The full raw record is preserved as ``original`` for the write-once, hashed immutable copy.
    """
    for key in _REQUIRED:
        if raw.get(key) in (None, ""):
            raise ValueError(f"lab result missing required field {key!r}")
    return {
        "instrument": str(raw["instrument"]),
        "sample_ref": str(raw["sample_ref"]),
        "element": str(raw["element"]),
        "value": _parse_value(raw["value"]),
        "unit": str(raw["unit"]),
        "ts": _parse_ts(raw["time"]),
        "method": (str(raw["method"]) if raw.get("method") not in (None, "") else None),
        "batch_ref": (str(raw["batch_ref"]) if raw.get("batch_ref") not in (None, "") else None),
        "source": str(raw.get("source", "spectraa")),
        # The immutable original — the whole raw record, preserved and hashed by the service.
        "original": dict(raw),
    }


def _row_to_raw(row: dict[str, str], *, instrument: str) -> dict[str, Any]:
    """Map one SpectrAA CSV row to a raw lab record — the vendor-format seam (``_to_raw``).

    Best-effort against a *plausible* SpectrAA export layout; the real column names are
    confirmed on site and changed **here only**. Instrument is a property of the run/file, not
    the row, so it is supplied by the caller.
    """
    return {
        "instrument": instrument,
        "sample_ref": row.get("Sample") or row.get("SampleID") or "",
        "element": row.get("Element") or "",
        "value": row.get("Concentration") or row.get("Conc") or "",
        "unit": row.get("Units") or row.get("Unit") or "",
        "time": row.get("DateTime") or row.get("Date/Time") or "",
        "method": row.get("Method") or None,
        "batch_ref": row.get("Batch") or row.get("BatchID") or None,
        "source": "spectraa_csv",
    }


def parse_spectraa_csv(text: str, *, instrument: str) -> list[dict[str, Any]]:
    """Parse a SpectrAA CSV export into raw lab records (no DB I/O).

    Direct ingestion of the instrument's own output — the "no manual retyping" capability. Row
    mapping is isolated to :func:`_row_to_raw`; validation happens downstream in the normaliser.
    """
    reader = csv.DictReader(io.StringIO(text))
    return [_row_to_raw(row, instrument=instrument) for row in reader]


def ingest_lab_results(
    session: Any,
    site_id: str,
    raw_records: list[dict[str, Any]],
    *,
    created_by: str,
    now: datetime | None = None,
    bounds: dict[str, tuple[float, float]] | None = None,
    commit: bool = True,
) -> tuple[list[LaboratoryResult], list[EventV1]]:
    """Normalise, record and anomaly-check a batch of raw lab records. Returns (new, alarms).

    Site-scoped and idempotent on the deterministic result id, so replays/backfill never
    double-record. A malformed record raises ``ValueError`` from the normaliser and aborts the
    batch before any commit — instrument data is never silently dropped.
    """
    new: list[LaboratoryResult] = []
    alarms: list[EventV1] = []
    for raw in raw_records:
        kwargs = normalise_lab_result(raw)
        row, created, evs = service.ingest_result(
            session, site_id=site_id, created_by=created_by, now=now, bounds=bounds, **kwargs
        )
        if created:
            new.append(row)
        alarms.extend(evs)
    if commit:
        session.commit()
    log.info(
        "lab results ingested",
        extra={
            "site_id": site_id,
            "received": len(raw_records),
            "new": len(new),
            "alarms": len(alarms),
        },
    )
    return new, alarms


# A small recorded set — the fixture role the simulator plays for GNSS. Times are explicit
# Africa/Harare offsets (+02:00). Plausible fire-assay/AAS gold-mine results.
SAMPLE_RESULTS: list[dict[str, Any]] = [
    {
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-1001",
        "element": "Au",
        "value": 3.42,
        "unit": "g/t",
        "time": "2026-09-12T09:15:00+02:00",
        "method": "fire_assay_aas",
        "batch_ref": "RUN-88",
        "source": "spectraa_csv",
    },
    {
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-1002",
        "element": "Au",
        "value": 0.87,
        "unit": "g/t",
        "time": "2026-09-12T09:22:00+02:00",
        "method": "fire_assay_aas",
        "batch_ref": "RUN-88",
        "source": "spectraa_csv",
    },
]

# A tiny SpectrAA-shaped CSV export, the "direct file ingestion" demonstration.
SAMPLE_CSV = (
    "Sample,Element,Concentration,Units,DateTime,Method,Batch\r\n"
    "S-2001,Au,5.10,g/t,2026-09-12T10:05:00+02:00,fire_assay_aas,RUN-89\r\n"
    "S-2002,Ag,12.4,g/t,2026-09-12T10:11:00+02:00,fire_assay_aas,RUN-89\r\n"
)


def main() -> None:
    """Replay the sample lab results into the configured database (a local demo)."""
    from minemonitor.config import get_settings
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_session_factory

    settings = get_settings()
    configure_logging(settings.log_level)
    bounds = service.parse_bounds(settings.lab_anomaly_bounds)
    factory = get_session_factory()
    with factory() as session:
        new, alarms = ingest_lab_results(
            session,
            settings.default_site_id,
            SAMPLE_RESULTS,
            created_by="simulator",
            bounds=bounds,
        )
    print(f"ingested {len(new)} lab results, raised {len(alarms)} alarms (replay is idempotent)")


if __name__ == "__main__":
    main()
