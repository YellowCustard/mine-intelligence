"""Dispatch domain service. Callers commit.

``recommend`` is decision-support: it produces advisory truck→job pairings with an
explicit ``rationale``, prioritised by job priority and truck availability. It never
claims optimality and never dispatches on its own — a supervisor approves a
recommendation to turn it into an instruction (``approve_assignment``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import Asset, DispatchAssignment, DispatchJob

_TRUCK_CLASS = "haul_truck"
_COMMITTED = ("approved", "active")


# --- jobs --------------------------------------------------------------------


def create_job(
    session: Session,
    *,
    site_id: str,
    created_by: str,
    now: datetime,
    material: str | None = None,
    source_zone: str | None = None,
    dest_zone: str | None = None,
    target_trucks: int | None = None,
    priority: int = 1,
    note: str | None = None,
) -> DispatchJob:
    if target_trucks is not None and target_trucks <= 0:
        raise ValueError("target_trucks must be positive")
    job = DispatchJob(
        id=str(ULID()),
        site_id=site_id,
        material=material,
        source_zone=source_zone,
        dest_zone=dest_zone,
        target_trucks=target_trucks,
        priority=priority,
        status="open",
        note=note,
        created_by=created_by,
        created_at=now,
    )
    session.add(job)
    return job


def list_jobs(session: Session, site_id: str, *, status: str | None = None) -> list[DispatchJob]:
    stmt = select(DispatchJob).where(DispatchJob.site_id == site_id)
    if status is not None:
        stmt = stmt.where(DispatchJob.status == status)
    stmt = stmt.order_by(DispatchJob.priority.desc(), DispatchJob.created_at)
    return list(session.execute(stmt).scalars().all())


def set_job_status(session: Session, site_id: str, job_id: str, status: str) -> DispatchJob | None:
    job = session.get(DispatchJob, job_id)
    if job is None or job.site_id != site_id:
        return None
    job.status = status
    return job


# --- recommendations ---------------------------------------------------------


def _next_job(
    jobs: list[DispatchJob], committed: Counter[str], pending: dict[str, int]
) -> DispatchJob | None:
    """Highest-priority open job still under its truck target (uncapped = always eligible)."""
    for job in jobs:
        if job.target_trucks is None:
            return job
        if committed.get(job.id, 0) + pending[job.id] < job.target_trucks:
            return job
    return None


def recommend(
    session: Session, site_id: str, *, now: datetime, objective: str = "balanced"
) -> list[DispatchAssignment]:
    """Generate advisory recommendations. Supersedes prior un-approved recommendations.

    Pairs each available haul truck (a fleet asset not committed to an approved/active
    job) with the highest-priority open job still under its truck target. Persists each
    as a ``recommended`` assignment with its evidence. Requires supervisor approval to
    become an instruction.
    """
    # Prior recommendations are ephemeral suggestions — clear the un-actioned ones.
    session.execute(
        delete(DispatchAssignment).where(
            DispatchAssignment.site_id == site_id, DispatchAssignment.state == "recommended"
        )
    )
    committed_rows = session.execute(
        select(DispatchAssignment.asset_id, DispatchAssignment.job_id).where(
            DispatchAssignment.site_id == site_id, DispatchAssignment.state.in_(_COMMITTED)
        )
    ).all()
    committed_asset_ids = {a for a, _ in committed_rows}
    committed_per_job: Counter[str] = Counter(j for _, j in committed_rows)

    trucks = list(
        session.execute(
            select(Asset.asset_id)
            .where(Asset.site_id == site_id, Asset.asset_class == _TRUCK_CLASS)
            .order_by(Asset.asset_id)
        ).scalars()
    )
    available = [t for t in trucks if t not in committed_asset_ids]
    jobs = list_jobs(session, site_id, status="open")

    pending: dict[str, int] = defaultdict(int)
    recs: list[DispatchAssignment] = []
    for truck in available:
        job = _next_job(jobs, committed_per_job, pending)
        if job is None:
            break
        rationale = [
            f"job priority {job.priority}" + (f", material {job.material}" if job.material else ""),
            f"{truck} available (not committed to an approved/active job)",
        ]
        if job.target_trucks is not None:
            have = committed_per_job.get(job.id, 0) + pending[job.id]
            rationale.append(f"job wants {job.target_trucks} trucks; {have} already assigned")
        rec = DispatchAssignment(
            id=str(ULID()),
            site_id=site_id,
            job_id=job.id,
            asset_id=truck,
            state="recommended",
            objective=objective,
            score=float(job.priority),
            rationale=rationale,
            recommended_at=now,
        )
        session.add(rec)
        recs.append(rec)
        pending[job.id] += 1
    return recs


def list_assignments(
    session: Session, site_id: str, *, state: str | None = None
) -> list[DispatchAssignment]:
    stmt = select(DispatchAssignment).where(DispatchAssignment.site_id == site_id)
    if state is not None:
        stmt = stmt.where(DispatchAssignment.state == state)
    stmt = stmt.order_by(DispatchAssignment.score.desc(), DispatchAssignment.recommended_at)
    return list(session.execute(stmt).scalars().all())


def approve_assignment(
    session: Session, site_id: str, assignment_id: str, *, approved_by: str, now: datetime
) -> DispatchAssignment | None:
    """Approve a recommended assignment (recommended → approved). Idempotent-safe: only a
    recommended assignment can be approved."""
    a = session.get(DispatchAssignment, assignment_id)
    if a is None or a.site_id != site_id:
        return None
    if a.state != "recommended":
        raise ValueError(f"only a recommended assignment can be approved (state={a.state})")
    a.state = "approved"
    a.approved_by = approved_by
    a.approved_at = now
    return a


def reject_assignment(
    session: Session, site_id: str, assignment_id: str
) -> DispatchAssignment | None:
    a = session.get(DispatchAssignment, assignment_id)
    if a is None or a.site_id != site_id:
        return None
    if a.state != "recommended":
        raise ValueError(f"only a recommended assignment can be rejected (state={a.state})")
    a.state = "rejected"
    return a


def as_recommendation_dict(a: DispatchAssignment) -> dict[str, Any]:
    """Shape a recommended assignment as a dispatch.recommendation.v1 payload."""
    return {
        "schema": "dispatch.recommendation.v1",
        "assignment_id": a.id,
        "site_id": a.site_id,
        "job_id": a.job_id,
        "asset_id": a.asset_id,
        "ts": a.recommended_at,
        "objective": a.objective,
        "score": a.score,
        "rationale": list(a.rationale),
        "advisory": True,
    }
