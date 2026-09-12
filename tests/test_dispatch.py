"""Dispatch domain: jobs, explainable recommendations, approval workflow, RBAC, bus."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from minemonitor.dispatch import service
from minemonitor.platform import contracts
from minemonitor.platform.bus import bus
from minemonitor.storage.models import Asset
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _trucks(db: Session, *ids: str) -> None:
    for a in ids:
        db.add(Asset(asset_id=a, site_id="kn-zw-01", asset_class="haul_truck"))
    db.commit()


# --- recommendation heuristic ------------------------------------------------


def test_recommendations_are_prioritised_and_capped(db_session: Session) -> None:
    _trucks(db_session, "HT-1", "HT-2", "HT-3")
    lo = service.create_job(
        db_session,
        site_id="kn-zw-01",
        created_by="s",
        now=_NOW,
        priority=1,
        target_trucks=1,
        material="waste",
    )
    hi = service.create_job(
        db_session,
        site_id="kn-zw-01",
        created_by="s",
        now=_NOW,
        priority=5,
        target_trucks=1,
        material="gold_ore",
    )
    db_session.commit()
    recs = service.recommend(db_session, "kn-zw-01", now=_NOW)
    db_session.commit()
    by_job = {r.job_id: r for r in recs}
    # Each capped job gets exactly its target; both jobs are served (2 trucks used, 1 spare).
    assert len(recs) == 2
    assert hi.id in by_job and lo.id in by_job
    assert all(r.state == "recommended" for r in recs)
    # The higher-priority job scores higher and its recommendation carries evidence.
    assert by_job[hi.id].score > by_job[lo.id].score
    assert any("priority 5" in line for line in by_job[hi.id].rationale)


def test_recommendation_skips_committed_trucks(db_session: Session) -> None:
    _trucks(db_session, "HT-1")  # plus the seeded HT-102 → 2 haul trucks
    service.create_job(db_session, site_id="kn-zw-01", created_by="s", now=_NOW, priority=3)
    db_session.commit()
    recs = service.recommend(db_session, "kn-zw-01", now=_NOW)
    committed = recs[0].asset_id
    service.approve_assignment(db_session, "kn-zw-01", recs[0].id, approved_by="s", now=_NOW)
    db_session.commit()
    # The approved truck is committed → a re-run never recommends it again.
    recs2 = service.recommend(db_session, "kn-zw-01", now=_NOW)
    db_session.commit()
    assert committed not in {r.asset_id for r in recs2}
    assert len(recs2) == len(recs) - 1


def test_recommend_supersedes_prior_recommendations(db_session: Session) -> None:
    # A capped job (1 truck) → each run makes exactly one recommendation.
    service.create_job(
        db_session, site_id="kn-zw-01", created_by="s", now=_NOW, priority=1, target_trucks=1
    )
    db_session.commit()
    service.recommend(db_session, "kn-zw-01", now=_NOW)
    db_session.commit()
    service.recommend(db_session, "kn-zw-01", now=_NOW)
    db_session.commit()
    # Only one live recommendation, not two (prior un-approved ones are cleared).
    assert len(service.list_assignments(db_session, "kn-zw-01", state="recommended")) == 1


def test_approve_only_from_recommended(db_session: Session) -> None:
    _trucks(db_session, "HT-1")
    service.create_job(db_session, site_id="kn-zw-01", created_by="s", now=_NOW, priority=1)
    db_session.commit()
    rec = service.recommend(db_session, "kn-zw-01", now=_NOW)[0]
    service.approve_assignment(db_session, "kn-zw-01", rec.id, approved_by="s", now=_NOW)
    db_session.commit()
    with pytest.raises(ValueError, match="only a recommended"):
        service.approve_assignment(db_session, "kn-zw-01", rec.id, approved_by="s", now=_NOW)


# --- contract / API ----------------------------------------------------------


def test_dispatch_contract_registered() -> None:
    assert "dispatch.recommendation.v1" in contracts.registered()


def test_full_workflow_over_api_and_bus(db_session: Session) -> None:
    _trucks(db_session, "HT-1", "HT-2")
    captured: list = []
    bus().subscribe("dispatch.recommendation.v1", lambda e: captured.append(e))
    try:
        sup = make_client(db_session, SUPERVISOR)
        sup.post("/sites/kn-zw-01/dispatch/jobs", json={"priority": 5, "material": "gold_ore"})
        recs = sup.post("/sites/kn-zw-01/dispatch/recommendations", json={}).json()
        assert recs and all(r["advisory"] is True and r["state"] == "recommended" for r in recs)
        # Each recommendation was published on the bus as an advisory event.
        assert len(captured) == len(recs) and all(e.advisory is True for e in captured)
        # Approve one → it becomes a dispatched instruction.
        r = sup.post(f"/sites/kn-zw-01/dispatch/assignments/{recs[0]['id']}/approve")
        assert r.status_code == 200 and r.json()["state"] == "approved"
        # Approving again conflicts (409).
        assert (
            sup.post(f"/sites/kn-zw-01/dispatch/assignments/{recs[0]['id']}/approve").status_code
            == 409
        )
    finally:
        bus().clear()


def test_rbac_and_v1_mount(db_session: Session) -> None:
    v = make_client(db_session, VIEWER)
    # Viewer cannot create jobs or recommend, but can read.
    assert v.post("/sites/kn-zw-01/dispatch/jobs", json={"priority": 1}).status_code == 403
    assert v.post("/sites/kn-zw-01/dispatch/recommendations", json={}).status_code == 403
    assert v.get("/sites/kn-zw-01/dispatch/jobs").status_code == 200
    assert v.get("/api/v1/sites/kn-zw-01/dispatch/assignments").status_code == 200


def test_job_creation_is_audited(db_session: Session) -> None:
    make_client(db_session, SUPERVISOR).post(
        "/sites/kn-zw-01/dispatch/jobs", json={"priority": 2, "material": "ore"}
    )
    audit = make_client(db_session, ADMIN).get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "dispatch.job.create" for a in audit)
