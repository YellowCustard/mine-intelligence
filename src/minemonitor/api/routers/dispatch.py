"""Dispatch domain API (Phase 6) — decision-support: jobs, recommendations, approvals.

Reads are viewer-level; creating jobs, generating recommendations, and approving them
are supervisor-level and audited. Every write is site-scoped. Generating recommendations
publishes ``dispatch.recommendation.v1`` on the in-process bus. Recommendations are
advisory: a supervisor must approve one before it is a dispatched instruction — the
platform never actuates a machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_supervisor, require_viewer
from minemonitor.contracts.dispatch import DispatchRecommendationV1
from minemonitor.dispatch import service
from minemonitor.platform.bus import bus
from minemonitor.storage.db import get_db
from minemonitor.storage.models import DispatchAssignment, DispatchJob, User

router = APIRouter(tags=["dispatch"])

_JOB_STATUSES = {"open", "active", "complete", "cancelled"}


class JobIn(BaseModel):
    material: str | None = None
    source_zone: str | None = None
    dest_zone: str | None = None
    target_trucks: int | None = Field(default=None, gt=0)
    priority: int = Field(default=1, ge=1)
    note: str | None = None


class JobStatusIn(BaseModel):
    status: str


class RecommendIn(BaseModel):
    objective: str = "balanced"


def _job_dict(j: DispatchJob) -> dict[str, Any]:
    return {
        "id": j.id,
        "material": j.material,
        "source_zone": j.source_zone,
        "dest_zone": j.dest_zone,
        "target_trucks": j.target_trucks,
        "priority": j.priority,
        "status": j.status,
        "note": j.note,
    }


def _assignment_dict(a: DispatchAssignment) -> dict[str, Any]:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "asset_id": a.asset_id,
        "state": a.state,
        "objective": a.objective,
        "score": a.score,
        "rationale": list(a.rationale),
        "recommended_at": a.recommended_at,
        "approved_by": a.approved_by,
        "approved_at": a.approved_at,
        "advisory": True,
    }


@router.post("/sites/{site_id}/dispatch/jobs", status_code=201)
def create_job(
    site_id: str,
    body: JobIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Create a haulage job (supervisor; audited)."""
    try:
        job = service.create_job(
            db,
            site_id=site_id,
            created_by=user.username,
            now=datetime.now(UTC),
            material=body.material,
            source_zone=body.source_zone,
            dest_zone=body.dest_zone,
            target_trucks=body.target_trucks,
            priority=body.priority,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="dispatch.job.create",
        entity_type="dispatch_job",
        entity_id=job.id,
        site_id=site_id,
        detail={"priority": job.priority, "material": job.material},
    )
    db.commit()
    return _job_dict(job)


@router.get("/sites/{site_id}/dispatch/jobs")
def list_jobs(
    site_id: str,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [_job_dict(j) for j in service.list_jobs(db, site_id, status=status)]


@router.post("/sites/{site_id}/dispatch/jobs/{job_id}/status")
def set_job_status(
    site_id: str,
    job_id: str,
    body: JobStatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Set a job's status (supervisor; audited)."""
    if body.status not in _JOB_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"status must be one of {sorted(_JOB_STATUSES)}"
        )
    job = service.set_job_status(db, site_id, job_id, body.status)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    audit.record(
        db,
        actor=user.username,
        action="dispatch.job.status",
        entity_type="dispatch_job",
        entity_id=job_id,
        site_id=site_id,
        detail={"status": body.status},
    )
    db.commit()
    return _job_dict(job)


@router.post("/sites/{site_id}/dispatch/recommendations", status_code=201)
def recommend(
    site_id: str,
    body: RecommendIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> list[dict[str, Any]]:
    """Generate advisory recommendations (supervisor; audited). Publishes each on the bus."""
    recs = service.recommend(db, site_id, now=datetime.now(UTC), objective=body.objective)
    audit.record(
        db,
        actor=user.username,
        action="dispatch.recommend",
        entity_type="site",
        entity_id=site_id,
        site_id=site_id,
        detail={"count": len(recs), "objective": body.objective},
    )
    db.commit()
    for rec in recs:
        bus().publish(DispatchRecommendationV1.model_validate(service.as_recommendation_dict(rec)))
    return [_assignment_dict(r) for r in recs]


@router.get("/sites/{site_id}/dispatch/assignments")
def list_assignments(
    site_id: str,
    state: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [_assignment_dict(a) for a in service.list_assignments(db, site_id, state=state)]


@router.post("/sites/{site_id}/dispatch/assignments/{assignment_id}/approve")
def approve_assignment(
    site_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Approve a recommendation → dispatched instruction (supervisor; audited)."""
    try:
        a = service.approve_assignment(
            db, site_id, assignment_id, approved_by=user.username, now=datetime.now(UTC)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assignment not found")
    audit.record(
        db,
        actor=user.username,
        action="dispatch.assignment.approve",
        entity_type="dispatch_assignment",
        entity_id=a.id,
        site_id=site_id,
        detail={"job_id": a.job_id, "asset_id": a.asset_id},
    )
    db.commit()
    return _assignment_dict(a)


@router.post("/sites/{site_id}/dispatch/assignments/{assignment_id}/reject")
def reject_assignment(
    site_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Reject a recommendation (supervisor; audited)."""
    try:
        a = service.reject_assignment(db, site_id, assignment_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assignment not found")
    audit.record(
        db,
        actor=user.username,
        action="dispatch.assignment.reject",
        entity_type="dispatch_assignment",
        entity_id=a.id,
        site_id=site_id,
    )
    db.commit()
    return _assignment_dict(a)
