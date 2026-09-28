"""Vision model registry — predictions stay traceable, weights never silently change. Callers commit.

``docs/VISION_ARCHITECTURE.md`` §6 and ``docs/VISION_MODEL_LICENSES.md``:

- A model is a ``(model_id, version)`` pinned to exact weights by **SHA-256**. A new model is a new
  version; nothing overwrites a registered row's weights. Rollback is a status change.
- Lifecycle: ``experimental → validation → staging → production → retired``.
- **Licence gate.** AGPL, GPL and non-commercial licences are refused outright, at any status —
  ``CLAUDE.md`` is explicit that Ultralytics YOLO (AGPL-3.0) must not enter this codebase. Only
  permissive licences (Apache-2.0 / MIT / BSD) or an explicitly accepted custom licence may reach
  ``production``.
- Metrics are **measured** (fps, latency, and — once a mining validation set exists — mAP) and
  recorded here; published COCO numbers are never copied in as if they described this mine.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import VisionModelRecord

STATUSES = ("experimental", "validation", "staging", "production", "retired")
PERMISSIVE = frozenset({"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause"})
# Custom licences cleared by an explicit recorded decision (VISION_MODEL_LICENSES.md §1a).
ACCEPTED_CUSTOM = frozenset({"DINOv3 License"})
_FORBIDDEN = re.compile(r"AGPL|(?<!L)GPL|\bNC\b|-NC-|non[- ]?commercial|research[- ]only", re.I)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(ValueError):
    """A registry operation the policy refuses."""


def row_id(model_id: str, version: str) -> str:
    return f"{model_id}@{version}"


def licence_forbidden(licence: str) -> bool:
    return bool(_FORBIDDEN.search(licence))


def production_eligible(licence: str) -> bool:
    return licence in PERMISSIVE or licence in ACCEPTED_CUSTOM


def get_model(session: Session, model_id: str, version: str) -> VisionModelRecord | None:
    return session.get(VisionModelRecord, row_id(model_id, version))


def list_models(session: Session) -> list[VisionModelRecord]:
    return list(
        session.execute(
            select(VisionModelRecord).order_by(VisionModelRecord.model_id, VisionModelRecord.version)
        )
        .scalars()
        .all()
    )


def register_model(
    session: Session,
    *,
    model_id: str,
    version: str,
    task: str,
    licence: str,
    weights_sha256: str,
    classes: list[str],
    architecture: str | None = None,
    weights_ref: str | None = None,
    source_url: str | None = None,
    training_dataset: str | None = None,
    input_size: list[int] | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> VisionModelRecord:
    """Register a model version at ``experimental``. Refuses forbidden licences and duplicates."""
    if licence_forbidden(licence):
        raise RegistryError(
            f"licence {licence!r} is forbidden (AGPL/GPL/non-commercial) — "
            "see CLAUDE.md §7 and docs/VISION_MODEL_LICENSES.md"
        )
    if not _SHA256.match(weights_sha256):
        raise RegistryError("weights_sha256 must be a lowercase hex SHA-256")
    if not classes:
        raise RegistryError("a model must declare the classes it detects")
    if get_model(session, model_id, version) is not None:
        raise RegistryError(f"{row_id(model_id, version)} already registered — publish a new version")
    now = now or datetime.now(UTC)
    row = VisionModelRecord(
        id=row_id(model_id, version),
        model_id=model_id,
        version=version,
        task=task,
        architecture=architecture,
        licence=licence,
        weights_sha256=weights_sha256,
        weights_ref=weights_ref,
        source_url=source_url,
        training_dataset=training_dataset,
        classes=classes,
        input_size=input_size,
        metrics=None,
        status="experimental",
        notes=notes,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def set_status(
    session: Session, model_id: str, version: str, status: str, *, now: datetime | None = None
) -> VisionModelRecord:
    """Move a model through its lifecycle. ``production`` requires a permissive/accepted licence."""
    if status not in STATUSES:
        raise RegistryError(f"unknown status {status!r}")
    row = get_model(session, model_id, version)
    if row is None:
        raise LookupError(f"{row_id(model_id, version)} not registered")
    if status == "production" and not production_eligible(row.licence):
        raise RegistryError(
            f"licence {row.licence!r} is not cleared for production "
            "(Apache-2.0/MIT/BSD or an accepted custom licence only)"
        )
    row.status = status
    row.updated_at = now or datetime.now(UTC)
    return row


def record_metrics(
    session: Session,
    model_id: str,
    version: str,
    metrics: dict[str, Any],
    *,
    now: datetime | None = None,
) -> VisionModelRecord:
    """Merge measured metrics (e.g. a ``bench`` run keyed by hardware profile)."""
    row = get_model(session, model_id, version)
    if row is None:
        raise LookupError(f"{row_id(model_id, version)} not registered")
    row.metrics = {**(row.metrics or {}), **metrics}
    row.updated_at = now or datetime.now(UTC)
    return row


def model_dict(row: VisionModelRecord) -> dict[str, Any]:
    return {
        "model_id": row.model_id,
        "version": row.version,
        "task": row.task,
        "architecture": row.architecture,
        "licence": row.licence,
        "production_eligible": production_eligible(row.licence),
        "weights_sha256": row.weights_sha256,
        "weights_ref": row.weights_ref,
        "source_url": row.source_url,
        "training_dataset": row.training_dataset,
        "classes": row.classes,
        "input_size": row.input_size,
        "metrics": row.metrics,
        "status": row.status,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
