"""Laboratory domain service (FP-08). Callers commit unless noted.

Three responsibilities, kept separate and audit-first:

- **Record** — ``record_result`` stores an assay result **write-once**. The raw original is
  preserved and hashed; re-importing the identical result is a no-op (idempotent), and a
  *different* payload arriving under the same id is refused as a mutation and **flagged**
  (``lab_result_conflict``) — the tamper-evidence guarantee made real.
- **Correct** — ``add_correction`` appends a correction that references a result without ever
  mutating it, carrying the actor and reason. ``effective_value`` reads the latest correction.
- **Detect** — ``check_anomaly`` flags a value outside configured deterministic bounds by
  raising an advisory ``lab_anomaly`` ``event.v1`` into the unified queue. It **flags, never
  alters** (brief §15): the measured record stands; a human reviews.

Provenance is explicit throughout: the stored value is ``measured``; a correction is an
``annotation``; an anomaly is a flag for review, not a conclusion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.storage.models import Event, LaboratoryCorrection, LaboratoryResult

log = logging.getLogger("minemonitor.laboratory")

SOURCE = "laboratory"


def canonical_hash(original: dict[str, Any]) -> str:
    """Hex SHA-256 of the canonicalised original record.

    Canonical form is JSON with sorted keys and no incidental whitespace, so the same record
    always hashes identically regardless of key order — the tamper-evidence anchor.
    """
    canonical = json.dumps(original, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def result_id(site_id: str, instrument: str, sample_ref: str, element: str, ts: datetime) -> str:
    """Deterministic primary key: one (sample, element) analysed at one time is one result.

    Keyed so replay/backfill of the same result is idempotent while a genuine re-assay of the
    sample (a different ``ts``) is a distinct result, not a silent overwrite.
    """
    stamp = ts.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"lab-{site_id}-{instrument}-{sample_ref}-{element}-{stamp}"


def parse_bounds(raw: str) -> dict[str, tuple[float, float]]:
    """Parse the configured anomaly bounds JSON ``{"Au": [min, max], ...}``.

    Blank/invalid config yields no bounds (anomaly detection simply off) rather than failing —
    a misconfigured bound must never block ingestion of a real result.
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("invalid lab_anomaly_bounds JSON; anomaly detection disabled")
        return {}
    bounds: dict[str, tuple[float, float]] = {}
    if isinstance(data, dict):
        for element, pair in data.items():
            if isinstance(pair, list | tuple) and len(pair) == 2:
                try:
                    lo, hi = float(pair[0]), float(pair[1])
                except (ValueError, TypeError):
                    continue
                bounds[str(element)] = (lo, hi)
    return bounds


def record_result(
    session: Session,
    *,
    site_id: str,
    instrument: str,
    sample_ref: str,
    element: str,
    value: float,
    unit: str,
    ts: datetime,
    original: dict[str, Any],
    created_by: str,
    now: datetime,
    method: str | None = None,
    batch_ref: str | None = None,
    original_file_ref: str | None = None,
    source: str = "spectraa",
) -> tuple[LaboratoryResult, bool, EventV1 | None]:
    """Store one assay result write-once. Returns (row, created, conflict_alarm). No commit.

    Idempotent on the deterministic id: an identical re-import (same original hash) returns the
    stored row untouched with ``created=False``. A *different* original under the same id is
    **not** written over the original — it is refused as a mutation and a ``lab_result_conflict``
    warning is raised (deduped per result), so tampering or a divergent re-export is surfaced,
    not silently absorbed.
    """
    rid = result_id(site_id, instrument, sample_ref, element, ts)
    digest = canonical_hash(original)
    existing = session.get(LaboratoryResult, rid)
    if existing is not None:
        if existing.original_hash == digest:
            return existing, False, None
        alarm = _conflict_event(session, site_id, existing, digest, now)
        return existing, False, alarm
    row = LaboratoryResult(
        id=rid,
        site_id=site_id,
        instrument=instrument,
        sample_ref=sample_ref,
        ts=ts,
        element=element,
        value=value,
        unit=unit,
        method=method,
        batch_ref=batch_ref,
        original=original,
        original_hash=digest,
        original_file_ref=original_file_ref,
        source=source,
        created_at=now,
        created_by=created_by,
    )
    session.add(row)
    return row, True, None


def _conflict_event(
    session: Session,
    site_id: str,
    existing: LaboratoryResult,
    incoming_hash: str,
    now: datetime,
) -> EventV1 | None:
    """Raise a deduped ``lab_result_conflict`` when a stored result is re-sent with a changed
    original — the write-once guarantee surfacing a divergent payload rather than overwriting.
    """
    ev_id = f"lab-conflict-{existing.id}"
    if _event_exists(session, ev_id):
        return None
    ev = EventV1(
        schema="event.v1",
        event_id=ev_id,
        site_id=site_id,
        ts=now,
        type="lab_result_conflict",
        severity="warning",
        asset_id=None,
        zone_id=None,
        source=f"{SOURCE}:{existing.instrument}",
        summary=(
            f"Divergent re-import of lab result {existing.id}: original preserved "
            "(stored value unchanged) — review the source export"
        ),
        detail={
            "result_id": existing.id,
            "stored_hash": existing.original_hash,
            "incoming_hash": incoming_hash,
        },
        evidence={"result_id": existing.id},
        advisory=True,
        state="open",
    )
    return _persist_deduped(session, ev)


def _event_exists(session: Session, event_id: str) -> bool:
    return session.get(Event, event_id) is not None


def _persist_deduped(session: Session, ev: EventV1) -> EventV1 | None:
    """Persist an event under a deterministic id, tolerating a concurrent duplicate.

    A savepoint'd flush turns a primary-key collision (two workers raising the same alarm)
    into the documented per-key dedup instead of a 500 at the caller's commit.
    """
    try:
        with session.begin_nested():
            persist_event(session, ev)
            session.flush()
        return ev
    except IntegrityError:
        return None


def check_anomaly(
    session: Session,
    result: LaboratoryResult,
    *,
    bounds: dict[str, tuple[float, float]],
    now: datetime,
) -> EventV1 | None:
    """Flag a measured value outside its element's configured bounds. No commit.

    Deterministic bounds first (models only later, on real history). Raises one advisory
    ``lab_anomaly`` ``event.v1`` (deduped per result) when the value is out of range; the
    measured record is never altered — a human reviews the flag (brief §15).
    """
    pair = bounds.get(result.element)
    if pair is None:
        return None
    lo, hi = pair
    if lo <= result.value <= hi:
        return None
    ev_id = f"lab-anomaly-{result.id}"
    if _event_exists(session, ev_id):
        return None
    ev = EventV1(
        schema="event.v1",
        event_id=ev_id,
        site_id=result.site_id,
        ts=now,
        type="lab_anomaly",
        severity="warning",
        asset_id=None,
        zone_id=None,
        source=f"{SOURCE}:{result.instrument}",
        summary=(
            f"{result.element} {result.value} {result.unit} on sample {result.sample_ref} "
            f"is outside the expected range [{lo}, {hi}] — review"
        ),
        detail={
            "result_id": result.id,
            "element": result.element,
            "value": result.value,
            "unit": result.unit,
            "expected_min": lo,
            "expected_max": hi,
        },
        evidence={"result_id": result.id},
        advisory=True,
        state="open",
    )
    return _persist_deduped(session, ev)


def ingest_result(
    session: Session,
    *,
    site_id: str,
    instrument: str,
    sample_ref: str,
    element: str,
    value: float,
    unit: str,
    ts: datetime,
    original: dict[str, Any],
    created_by: str,
    now: datetime | None = None,
    method: str | None = None,
    batch_ref: str | None = None,
    original_file_ref: str | None = None,
    source: str = "spectraa",
    bounds: dict[str, tuple[float, float]] | None = None,
) -> tuple[LaboratoryResult, bool, list[EventV1]]:
    """Record a result and run anomaly detection over a newly-stored one. No commit.

    Returns (row, created, alarms). A duplicate never re-alarms; a divergent re-import raises
    a conflict flag. A freshly recorded value outside its element's bounds raises an anomaly
    flag. Both are advisory ``event.v1`` in the unified queue.
    """
    now = now or datetime.now(UTC)
    row, created, conflict = record_result(
        session,
        site_id=site_id,
        instrument=instrument,
        sample_ref=sample_ref,
        element=element,
        value=value,
        unit=unit,
        ts=ts,
        original=original,
        created_by=created_by,
        now=now,
        method=method,
        batch_ref=batch_ref,
        original_file_ref=original_file_ref,
        source=source,
    )
    alarms: list[EventV1] = []
    if conflict is not None:
        alarms.append(conflict)
    if created and bounds:
        anomaly = check_anomaly(session, row, bounds=bounds, now=now)
        if anomaly is not None:
            alarms.append(anomaly)
    return row, created, alarms


def add_correction(
    session: Session,
    *,
    site_id: str,
    result_id_: str,
    corrected_value: float,
    actor: str,
    reason: str,
    now: datetime | None = None,
    unit: str | None = None,
) -> LaboratoryCorrection | None:
    """Append a correction to a result (never a mutation). No commit.

    Returns the new correction, or ``None`` if the result is unknown for this site. The
    original ``LaboratoryResult`` is untouched — the corrected value is read via
    ``effective_value``, and the full correction history is retained.
    """
    now = now or datetime.now(UTC)
    result = session.get(LaboratoryResult, result_id_)
    if result is None or result.site_id != site_id:
        return None
    row = LaboratoryCorrection(
        id=str(ULID()),
        site_id=site_id,
        result_id=result_id_,
        corrected_value=corrected_value,
        unit=unit,
        actor=actor,
        reason=reason,
        ts=now,
        created_at=now,
    )
    session.add(row)
    return row


def list_corrections(session: Session, site_id: str, result_id_: str) -> list[LaboratoryCorrection]:
    """The correction history for a result, oldest first. Site-scoped."""
    stmt = (
        select(LaboratoryCorrection)
        .where(
            LaboratoryCorrection.site_id == site_id,
            LaboratoryCorrection.result_id == result_id_,
        )
        .order_by(LaboratoryCorrection.ts.asc())
    )
    return list(session.execute(stmt).scalars().all())


def effective_value(session: Session, site_id: str, result_id_: str) -> tuple[float, str] | None:
    """The current value of a result: the latest correction if any, else the measured value.

    Returns ``(value, basis)`` where basis is ``corrected`` or ``measured``, or ``None`` if the
    result is unknown for this site.
    """
    result = session.get(LaboratoryResult, result_id_)
    if result is None or result.site_id != site_id:
        return None
    corrections = list_corrections(session, site_id, result_id_)
    if corrections:
        return corrections[-1].corrected_value, "corrected"
    return result.value, "measured"


def list_results(
    session: Session,
    site_id: str,
    *,
    sample_ref: str | None = None,
    element: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[LaboratoryResult]:
    """List assay results for a site (always site-scoped), newest first."""
    stmt = select(LaboratoryResult).where(LaboratoryResult.site_id == site_id)
    if sample_ref is not None:
        stmt = stmt.where(LaboratoryResult.sample_ref == sample_ref)
    if element is not None:
        stmt = stmt.where(LaboratoryResult.element == element)
    if since is not None:
        stmt = stmt.where(LaboratoryResult.ts >= since)
    if until is not None:
        stmt = stmt.where(LaboratoryResult.ts < until)
    stmt = stmt.order_by(LaboratoryResult.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())
