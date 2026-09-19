"""Gold-reconciliation groundwork (FP-09): measurement ingestion — idempotency, validation,
site-scoping, RBAC. No expected/variance/theft conclusion exists yet (deferred slices)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from minemonitor.platform import contracts
from minemonitor.reconciliation import service
from minemonitor.storage.models import MaterialMeasurement
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
_TS = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 12, 9, 5, tzinfo=UTC)


def _record(db: Session, **over: object):
    kw: dict = {
        "site_id": SITE,
        "stage": "crusher",
        "ts": _TS,
        "quantity": 128.5,
        "unit": "t",
        "source_ref": "W-500",
        "created_by": "op",
        "now": _NOW,
        "source": "weightometer",
    }
    kw.update(over)
    return service.record_measurement(db, **kw)


# -- record: idempotency + validation --------------------------------------------------


def test_record_and_idempotent_replay(db_session: Session) -> None:
    row1, created1 = _record(db_session)
    db_session.commit()
    row2, created2 = _record(db_session)
    db_session.commit()
    assert created1 is True and created2 is False and row1.id == row2.id
    assert db_session.query(MaterialMeasurement).count() == 1


def test_rejects_unknown_stage(db_session: Session) -> None:
    import pytest

    with pytest.raises(ValueError, match="stage"):
        _record(db_session, stage="smelter")


def test_rejects_negative_quantity(db_session: Session) -> None:
    import pytest

    with pytest.raises(ValueError, match="non-negative"):
        _record(db_session, quantity=-1.0)


def test_measurement_id_is_deterministic() -> None:
    a = service.measurement_id(SITE, "crusher", "weightometer", "W-500")
    b = service.measurement_id(SITE, "crusher", "weightometer", "W-500")
    assert a == b == f"mm-{SITE}-crusher-weightometer-W-500"


# -- list: filters + site-scoping -----------------------------------------------------


def test_list_filters_by_stage_and_site(db_session: Session) -> None:
    _record(db_session, stage="crusher", source_ref="W-1")
    _record(db_session, stage="mill", source_ref="W-2")
    _record(db_session, site_id="other-site", stage="crusher", source_ref="W-3")
    db_session.commit()
    crusher = service.list_measurements(db_session, SITE, stage="crusher")
    assert len(crusher) == 1 and crusher[0].stage == "crusher"
    assert len(service.list_measurements(db_session, SITE)) == 2  # site-scoped, excludes other-site


def test_contract_registered() -> None:
    assert "material.measurement.v1" in contracts.registered()


# -- API + RBAC -----------------------------------------------------------------------


def test_api_ingest_and_list(db_session: Session) -> None:
    client = make_client(db_session, SUPERVISOR)
    body = {
        "stage": "output",
        "time": "2026-09-12T09:15:00+02:00",
        "quantity": 3.4,
        "unit": "kg",
        "source_ref": "POUR-12",
        "source": "production",
    }
    r = client.post(f"/api/v1/sites/{SITE}/reconciliation/measurements", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["measurement"]["provenance"] == "measured" and r.json()["created"] is True
    g = client.get(f"/api/v1/sites/{SITE}/reconciliation/measurements?stage=output")
    assert g.status_code == 200 and len(g.json()) == 1


def test_api_rejects_bad_stage(db_session: Session) -> None:
    client = make_client(db_session, ADMIN)
    r = client.post(
        f"/api/v1/sites/{SITE}/reconciliation/measurements",
        json={
            "stage": "smelter",
            "time": "2026-09-12T09:15:00+02:00",
            "quantity": 1.0,
            "unit": "t",
            "source_ref": "X",
        },
    )
    assert r.status_code == 422  # request-model enum guard


def test_api_rejects_naive_timestamp(db_session: Session) -> None:
    client = make_client(db_session, ADMIN)
    r = client.post(
        f"/api/v1/sites/{SITE}/reconciliation/measurements",
        json={
            "stage": "crusher",
            "time": "2026-09-12T09:15:00",
            "quantity": 1.0,
            "unit": "t",
            "source_ref": "X",
        },
    )
    assert r.status_code == 400


def test_api_viewer_cannot_ingest(db_session: Session) -> None:
    client = make_client(db_session, VIEWER)
    r = client.post(
        f"/api/v1/sites/{SITE}/reconciliation/measurements",
        json={
            "stage": "crusher",
            "time": "2026-09-12T09:15:00+02:00",
            "quantity": 1.0,
            "unit": "t",
            "source_ref": "X",
        },
    )
    assert r.status_code in (401, 403)
