"""Vision model registry: predictions stay traceable, weights never silently change.

Callers commit.

``docs/VISION_ARCHITECTURE.md`` §6 and ``docs/VISION_MODEL_LICENSES.md``:

- A model is a ``(model_id, version)`` pinned to exact weights by **SHA-256**. A new model is a new
  version; nothing overwrites a registered row's weights. Rollback is a status change.
- Lifecycle: ``experimental → validation → staging → production → retired``.
- **Licence gate.** AGPL, GPL and non-commercial licences are refused outright, at any status —
  ``CLAUDE.md`` is explicit that Ultralytics YOLO (AGPL-3.0) must not enter this codebase. Only
  permissive licences (Apache-2.0 / MIT / BSD) or an explicitly accepted custom licence may reach
  ``production``.
- **The licence follows the model and size, not the string typed in.** A licence is per model,
  per size and per release (RF-DETR's repository is Apache-2.0, its XL and 2XL weights are not),
  so the declared licence is checked against ``AUDITED``, the licence audit's rows as the gate
  reads them. A declared licence the audit contradicts is refused at registration, and only an
  audited model and size at its audited, cleared licence reaches ``production``.
- Metrics are **measured** (fps, latency, and — once a mining validation set exists — mAP) and
  recorded here; published COCO numbers are never copied in as if they described this mine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class AuditedModel:
    """One row of ``docs/VISION_MODEL_LICENSES.md``, as the gate reads it."""

    pattern: re.Pattern[str]  # full match on the lowercased architecture, "<model>-<size>"
    licence: str  # code and weights, as audited
    source: str  # its section of docs/VISION_MODEL_LICENSES.md


def _audited(pattern: str, licence: str, source: str) -> AuditedModel:
    return AuditedModel(re.compile(pattern), licence, source)


# Deny by default: an architecture no row matches never reaches production. A row is added here
# when its model and size are audited (code, weights and training data, VISION_MODEL_LICENSES.md
# §8), not when the family looks permissive.
AUDITED: tuple[AuditedModel, ...] = (
    # Cleared.
    _audited(r"yolox-(nano|tiny|s|m|l|x)", "Apache-2.0", "§1"),
    _audited(r"rfdetr-(nano|small|medium|large)", "Apache-2.0", "§1"),
    _audited(r"dinov2-(vits14|vitb14|vitl14|vitg14)", "Apache-2.0", "§1a"),
    # Accepted by recorded decision; the web-image (LVD-1689M) checkpoints only.
    _audited(
        r"dinov3-(vits16|vits16plus|vitb16|vitl16|vith16plus|vit7b16|convnext-(tiny|small|base|large))",
        "DINOv3 License",
        "§1a",
    ),
    # Not cleared, whatever the family's repository says.
    _audited(r"rfdetr-(atto|femto|pico|xl|2xl)", "PML 1.0", "§1"),
    _audited(r"d-fine-.+-(obj365|obj2coco)", "Objects365 dataset terms", "§1"),
    # Forbidden.
    _audited(r"(yolov5|yolov8|yolov11|yolo11|yoloe|fastsam)[a-z0-9.-]*", "AGPL-3.0", "§1, §2, §4"),
    _audited(r"(yolov7|yolo-world)[a-z0-9.-]*", "GPL-3.0", "§1, §4"),
    _audited(r"yolo-nas[a-z0-9.-]*", "non-commercial weights", "§1"),
    _audited(r"depth-anything-v2-(base|large)", "CC-BY-NC-4.0", "§5"),
    _audited(r"deimv2[a-z0-9.-]*", "DEIMv2 License (non-commercial)", "§1"),
    _audited(
        r"(edgecrafter|ecdet)[a-z0-9.-]*",
        "EdgeCrafter License (non-commercial)",
        "§1",
    ),
)


class RegistryError(ValueError):
    """A registry operation the policy refuses."""


def row_id(model_id: str, version: str) -> str:
    return f"{model_id}@{version}"


def licence_forbidden(licence: str) -> bool:
    return bool(_FORBIDDEN.search(licence))


def audited(key: str) -> AuditedModel | None:
    """The audit row for a model and size (``"rfdetr-small"``), or ``None`` if it has none."""
    key = key.strip().lower()
    return next((a for a in AUDITED if a.pattern.fullmatch(key)), None)


def production_block(key: str | None, licence: str) -> str | None:
    """Why this model may not reach ``production``, or ``None`` if it may."""
    if not key or (row := audited(key)) is None:
        return (
            f"{key!r} is not an audited model and size — production needs its row in "
            "docs/VISION_MODEL_LICENSES.md and in registry.AUDITED"
        )
    if row.licence != licence:
        return (
            f"{key} is audited as {row.licence} (docs/VISION_MODEL_LICENSES.md {row.source}), "
            f"but registered as {licence!r}"
        )
    if licence not in PERMISSIVE and licence not in ACCEPTED_CUSTOM:
        return (
            f"{key} is {licence} (docs/VISION_MODEL_LICENSES.md {row.source}), not cleared for "
            "production (Apache-2.0/MIT/BSD or an accepted custom licence only)"
        )
    return None


def _key(row: VisionModelRecord) -> str:
    """The model and size the row runs: its architecture, or failing that its model id."""
    return row.architecture or row.model_id


def get_model(session: Session, model_id: str, version: str) -> VisionModelRecord | None:
    return session.get(VisionModelRecord, row_id(model_id, version))


def list_models(session: Session) -> list[VisionModelRecord]:
    return list(
        session.execute(
            select(VisionModelRecord).order_by(
                VisionModelRecord.model_id, VisionModelRecord.version
            )
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
    """Register a model version at ``experimental``.

    Refuses forbidden licences, a licence the audit contradicts, and duplicates.
    """
    if licence_forbidden(licence):
        raise RegistryError(
            f"licence {licence!r} is forbidden (AGPL/GPL/non-commercial) — "
            "see CLAUDE.md §7 and docs/VISION_MODEL_LICENSES.md"
        )
    # Both names are checked: a forbidden model is refused under either.
    for key in filter(None, (architecture, model_id)):
        audit = audited(key)
        if audit is None:
            continue
        where = f"docs/VISION_MODEL_LICENSES.md {audit.source}"
        if licence_forbidden(audit.licence):
            raise RegistryError(
                f"{key.strip()} is {audit.licence} ({where}), which is forbidden whatever "
                "licence is declared — see CLAUDE.md §7"
            )
        if audit.licence != licence:
            raise RegistryError(
                f"{key.strip()} is {audit.licence} ({where}), not {licence!r} — "
                "declare the audited licence"
            )
    if not _SHA256.match(weights_sha256):
        raise RegistryError("weights_sha256 must be a lowercase hex SHA-256")
    if not classes:
        raise RegistryError("a model must declare the classes it detects")
    if get_model(session, model_id, version) is not None:
        raise RegistryError(
            f"{row_id(model_id, version)} already registered — publish a new version"
        )
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
    """Move a model through its lifecycle. ``production`` requires an audited, cleared model."""
    if status not in STATUSES:
        raise RegistryError(f"unknown status {status!r}")
    row = get_model(session, model_id, version)
    if row is None:
        raise LookupError(f"{row_id(model_id, version)} not registered")
    if status == "production" and (block := production_block(_key(row), row.licence)):
        raise RegistryError(block)
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
    block = production_block(_key(row), row.licence)
    return {
        "model_id": row.model_id,
        "version": row.version,
        "task": row.task,
        "architecture": row.architecture,
        "licence": row.licence,
        "production_eligible": block is None,
        "production_block": block,
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
