"""Generator monitoring (FP-11): normalisation, idempotent readings, fault/unexpected-run
alarms, run-hours health reuse, and API + RBAC."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from minemonitor.generator import service
from minemonitor.ingest.adapters.dse_sim import (
    SAMPLE_READINGS,
    ingest_readings,
    normalise_reading,
)
from minemonitor.maintenance import service as maint
from minemonitor.platform import contracts
from minemonitor.storage.models import Event, GeneratorReading
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
_NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "asset_id": "GEN-01",
        "time": "2026-09-12T06:00:00+02:00",
        "running": True,
        "engine_hours": 1240.5,
        "fuel_level_pct": 82.0,
        "expected_running": True,
        "fault": False,
        "source": "dse_sim",
    }
    base.update(over)
    return base


# -- normalisation --------------------------------------------------------------------


def test_normalise_maps_fields() -> None:
    kw = normalise_reading(_raw())
    assert kw["asset_id"] == "GEN-01" and kw["running"] is True
    assert kw["engine_hours"] == 1240.5 and kw["fuel_level_pct"] == 82.0
    assert kw["ts"].tzinfo is not None and kw["expected_running"] is True


def test_normalise_rejects_missing_and_bad() -> None:
    for key in ("asset_id", "time", "running"):
        bad = _raw()
        del bad[key]
        with pytest.raises(ValueError, match=key):
            normalise_reading(bad)
    with pytest.raises(ValueError, match="numeric"):
        normalise_reading(_raw(engine_hours="lots"))
    with pytest.raises(ValueError, match="timezone-aware"):
        normalise_reading(_raw(time="2026-09-12T06:00:00"))


# -- idempotent record + alarms -------------------------------------------------------


def test_record_is_idempotent(db_session: Session) -> None:
    row1, created1, _ = service.record_reading(
        db_session, site_id=SITE, **normalise_reading(_raw())
    )
    db_session.commit()
    row2, created2, _ = service.record_reading(
        db_session, site_id=SITE, **normalise_reading(_raw())
    )
    db_session.commit()
    assert created1 is True and created2 is False and row1.id == row2.id
    assert db_session.query(GeneratorReading).count() == 1


def test_fault_raises_one_alarm_deduped(db_session: Session) -> None:
    _, _, alarms = service.record_reading(
        db_session, site_id=SITE, **normalise_reading(_raw(fault=True, fault_code="LOW_OIL"))
    )
    db_session.commit()
    assert len(alarms) == 1 and alarms[0].type == "generator_fault"
    assert alarms[0].severity == "critical" and alarms[0].advisory is True
    # Replay: no new reading, no new alarm.
    _, created, alarms2 = service.record_reading(
        db_session, site_id=SITE, **normalise_reading(_raw(fault=True, fault_code="LOW_OIL"))
    )
    db_session.commit()
    assert created is False and alarms2 == []
    faults = [e for e in db_session.query(Event).all() if e.type == "generator_fault"]
    assert len(faults) == 1


def test_unexpected_run_raises_warning(db_session: Session) -> None:
    _, _, alarms = service.record_reading(
        db_session, site_id=SITE, **normalise_reading(_raw(expected_running=False, running=True))
    )
    db_session.commit()
    assert len(alarms) == 1 and alarms[0].type == "generator_unexpected_run"
    assert alarms[0].severity == "warning"


def test_normal_running_raises_nothing(db_session: Session) -> None:
    _, _, alarms = service.record_reading(db_session, site_id=SITE, **normalise_reading(_raw()))
    db_session.commit()
    assert alarms == []


# -- engine-hours + health reuse ------------------------------------------------------


def test_latest_engine_hours_skips_nulls(db_session: Session) -> None:
    service.record_reading(db_session, site_id=SITE, **normalise_reading(_raw(engine_hours=100.0)))
    service.record_reading(
        db_session,
        site_id=SITE,
        **normalise_reading(_raw(time="2026-09-12T07:00:00+02:00", engine_hours=None)),
    )
    db_session.commit()
    assert service.latest_engine_hours(db_session, SITE, "GEN-01") == 100.0


def test_health_unknown_without_plan(db_session: Session) -> None:
    service.record_reading(db_session, site_id=SITE, **normalise_reading(_raw(engine_hours=500.0)))
    db_session.commit()
    h = service.generator_health(db_session, SITE, "GEN-01", now=_NOW)
    assert h["risk"] == "Unknown" and h["schema"] == "maintenance.health.v1"


def test_health_uses_latest_hours_with_plan(db_session: Session) -> None:
    # A 250h service interval, last serviced at 1000h; latest reading 1200h → 200/250 = Elevated.
    maint.upsert_plan(
        db_session,
        site_id=SITE,
        asset_id="GEN-01",
        component="engine",
        interval_hours=250.0,
        interval_days=None,
        now=_NOW,
    )
    wo = maint.open_work_order(
        db_session,
        site_id=SITE,
        asset_id="GEN-01",
        component="engine",
        type="service",
        created_by="tech",
        now=_NOW,
        at_engine_hours=1000.0,
    )
    maint.complete_work_order(db_session, SITE, wo.id, now=_NOW, at_engine_hours=1000.0)
    service.record_reading(db_session, site_id=SITE, **normalise_reading(_raw(engine_hours=1200.0)))
    db_session.commit()
    h = service.generator_health(db_session, SITE, "GEN-01", now=_NOW)
    assert h["dimension"] == "hours" and h["basis"] == "measured"
    assert h["risk"] == "Watch"  # 200/250 = 0.8 → [0.75, 0.90) Watch band


# -- batch ingest + contract ----------------------------------------------------------


def test_batch_ingest_and_replay(db_session: Session) -> None:
    new, alarms = ingest_readings(db_session, SITE, SAMPLE_READINGS)
    assert len(new) == 3
    types = sorted(a.type for a in alarms)
    assert types == ["generator_fault", "generator_unexpected_run"]
    new2, alarms2 = ingest_readings(db_session, SITE, SAMPLE_READINGS)
    assert new2 == [] and alarms2 == []


def test_contract_registered() -> None:
    assert "generator.reading.v1" in contracts.registered()


# -- API + RBAC -----------------------------------------------------------------------


def test_api_ingest_and_list(db_session: Session) -> None:
    client = make_client(db_session, SUPERVISOR)
    r = client.post(
        f"/api/v1/sites/{SITE}/generators/GEN-01/readings",
        json={
            "time": "2026-09-12T06:00:00+02:00",
            "running": True,
            "engine_hours": 1240.5,
            "fault": True,
            "fault_code": "LOW_OIL",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["alarms_raised"] == 1 and r.json()["reading"]["measured"] is True
    g = client.get(f"/api/v1/sites/{SITE}/generators/GEN-01/readings")
    assert g.status_code == 200 and len(g.json()) == 1


def test_api_health(db_session: Session) -> None:
    client = make_client(db_session, VIEWER)
    h = client.get(f"/api/v1/sites/{SITE}/generators/GEN-01/health")
    assert h.status_code == 200 and h.json()["risk"] == "Unknown"  # no plan/readings


def test_api_viewer_cannot_ingest(db_session: Session) -> None:
    client = make_client(db_session, VIEWER)
    r = client.post(
        f"/api/v1/sites/{SITE}/generators/GEN-01/readings",
        json={"time": "2026-09-12T06:00:00+02:00", "running": True},
    )
    assert r.status_code in (401, 403)


def test_api_rejects_naive_timestamp(db_session: Session) -> None:
    client = make_client(db_session, ADMIN)
    r = client.post(
        f"/api/v1/sites/{SITE}/generators/GEN-01/readings",
        json={"time": "2026-09-12T06:00:00", "running": True},
    )
    assert r.status_code == 400
