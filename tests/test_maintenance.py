"""Maintenance domain: deterministic health indicator, work orders, RBAC, contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from minemonitor.contracts.maintenance import MaintenanceHealthV1
from minemonitor.maintenance import service
from minemonitor.platform import contracts
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _plan(db: Session, **kw: object) -> None:
    service.upsert_plan(
        db, site_id="kn-zw-01", asset_id="HT-102", component="machine", now=_NOW, **kw
    )  # type: ignore[arg-type]


# --- deterministic health indicator ------------------------------------------


def test_no_plan_is_unknown(db_session: Session) -> None:
    a = service.assess_health(db_session, "kn-zw-01", "HT-102", now=_NOW)
    assert a["risk"] == "Unknown" and a["basis"] == "unknown" and a["confidence"] == 0.0
    assert a["inferred"] is True


def test_days_dimension_bands_risk_from_plan_start(db_session: Session) -> None:
    # 30-day interval, plan created 28 days ago, no service yet → 0.933 → Elevated.
    service.upsert_plan(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="machine",
        interval_hours=None,
        interval_days=30,
        now=_NOW - timedelta(days=28),
    )
    db_session.commit()
    a = service.assess_health(db_session, "kn-zw-01", "HT-102", now=_NOW)
    assert a["dimension"] == "days" and a["basis"] == "observed"
    assert a["risk"] == "Elevated" and 0.9 <= a["fraction"] < 1.0
    assert a["remaining"] == pytest.approx(2.0, abs=0.5)


def test_completed_service_resets_the_clock(db_session: Session) -> None:
    service.upsert_plan(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="machine",
        interval_hours=None,
        interval_days=30,
        now=_NOW - timedelta(days=90),
    )
    wo = service.open_work_order(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="machine",
        type="service",
        created_by="s",
        now=_NOW - timedelta(days=2),
    )
    service.complete_work_order(db_session, "kn-zw-01", wo.id, now=_NOW - timedelta(days=2))
    db_session.commit()
    a = service.assess_health(db_session, "kn-zw-01", "HT-102", now=_NOW)
    # 2 days since the service, 30-day interval → Normal.
    assert a["risk"] == "Normal" and a["fraction"] < 0.1


def test_hours_dimension_is_measured_and_wins_when_more_urgent(db_session: Session) -> None:
    service.upsert_plan(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="engine",
        interval_hours=500,
        interval_days=365,
        now=_NOW - timedelta(days=10),
    )
    wo = service.open_work_order(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="engine",
        type="service",
        created_by="s",
        now=_NOW - timedelta(days=10),
        at_engine_hours=1000.0,
    )
    service.complete_work_order(
        db_session, "kn-zw-01", wo.id, now=_NOW - timedelta(days=10), at_engine_hours=1000.0
    )
    db_session.commit()
    # 1600 current − 1000 baseline = 600h over a 500h interval → 1.2 → Critical (hours wins).
    a = service.assess_health(
        db_session, "kn-zw-01", "HT-102", "engine", now=_NOW, current_engine_hours=1600.0
    )
    assert a["dimension"] == "hours" and a["basis"] == "measured"
    assert a["risk"] == "Critical" and a["fraction"] == pytest.approx(1.2, abs=0.01)


def test_hours_plan_without_reading_is_unknown(db_session: Session) -> None:
    service.upsert_plan(
        db_session,
        site_id="kn-zw-01",
        asset_id="HT-102",
        component="engine",
        interval_hours=500,
        interval_days=None,
        now=_NOW,
    )
    db_session.commit()
    a = service.assess_health(db_session, "kn-zw-01", "HT-102", "engine", now=_NOW)
    assert a["risk"] == "Unknown"  # no engine-hours baseline/reading → never fabricated
    assert any("engine-hours" in e or "baseline" in e for e in a["evidence"])


def test_assessment_conforms_to_contract(db_session: Session) -> None:
    a = service.assess_health(db_session, "kn-zw-01", "HT-102", now=_NOW)
    MaintenanceHealthV1.model_validate(a)  # response shape matches the published contract


def test_plan_requires_an_interval(db_session: Session) -> None:
    with pytest.raises(ValueError, match="interval"):
        service.upsert_plan(
            db_session,
            site_id="kn-zw-01",
            asset_id="HT-102",
            component="machine",
            interval_hours=None,
            interval_days=None,
            now=_NOW,
        )


# --- contract / API ----------------------------------------------------------


def test_maintenance_contract_registered() -> None:
    assert "maintenance.health.v1" in contracts.registered()


def test_plan_admin_only_workorder_supervisor_health_viewer(db_session: Session) -> None:
    # Plans are admin config.
    assert (
        make_client(db_session, SUPERVISOR)
        .post("/sites/kn-zw-01/maintenance/plans", json={"asset_id": "HT-102", "interval_days": 30})
        .status_code
        == 403
    )
    assert (
        make_client(db_session, ADMIN)
        .post("/sites/kn-zw-01/maintenance/plans", json={"asset_id": "HT-102", "interval_days": 30})
        .status_code
        == 201
    )
    # Work orders are supervisor; viewer denied.
    assert (
        make_client(db_session, VIEWER)
        .post(
            "/sites/kn-zw-01/maintenance/work-orders",
            json={"asset_id": "HT-102", "type": "service"},
        )
        .status_code
        == 403
    )
    # Health is viewer-readable, at both path styles.
    v = make_client(db_session, VIEWER)
    r = v.get("/sites/kn-zw-01/maintenance/health", params={"asset_id": "HT-102"})
    assert r.status_code == 200 and r.json()["schema"] == "maintenance.health.v1"
    assert (
        v.get(
            "/api/v1/sites/kn-zw-01/maintenance/health", params={"asset_id": "HT-102"}
        ).status_code
        == 200
    )


def test_workorder_lifecycle_and_audit(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    admin.post(
        "/sites/kn-zw-01/maintenance/plans", json={"asset_id": "HT-102", "interval_days": 30}
    )
    sup = make_client(db_session, SUPERVISOR)
    wo = sup.post(
        "/sites/kn-zw-01/maintenance/work-orders", json={"asset_id": "HT-102", "type": "service"}
    ).json()
    r = sup.post(f"/sites/kn-zw-01/maintenance/work-orders/{wo['id']}/complete", json={})
    assert r.status_code == 200 and r.json()["status"] == "done"
    audit = admin.get("/sites/kn-zw-01/audit").json()
    actions = {a["action"] for a in audit}
    assert {
        "maintenance.plan.upsert",
        "maintenance.work_order.open",
        "maintenance.work_order.complete",
    } <= actions
