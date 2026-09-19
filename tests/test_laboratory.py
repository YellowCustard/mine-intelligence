"""Laboratory data ingestion (FP-08): normalisation, write-once + hash immutability,
append-only corrections, deterministic anomaly flagging, idempotency, API + RBAC."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from minemonitor.ingest.adapters.spectraa import (
    SAMPLE_CSV,
    SAMPLE_RESULTS,
    ingest_lab_results,
    normalise_lab_result,
    parse_spectraa_csv,
)
from minemonitor.laboratory import service
from minemonitor.platform import contracts
from minemonitor.storage.models import Event, LaboratoryResult
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
_NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
_BOUNDS = {"Au": (0.0, 100.0)}


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-1001",
        "element": "Au",
        "value": 3.42,
        "unit": "g/t",
        "time": "2026-09-12T09:15:00+02:00",
        "method": "fire_assay_aas",
        "batch_ref": "RUN-88",
        "source": "spectraa_csv",
    }
    base.update(over)
    return base


# -- normalisation --------------------------------------------------------------------


def test_normalise_maps_fields() -> None:
    kw = normalise_lab_result(_raw())
    assert kw["instrument"] == "agilent-aa-2000" and kw["sample_ref"] == "S-1001"
    assert kw["element"] == "Au" and kw["value"] == 3.42 and kw["unit"] == "g/t"
    assert kw["ts"].tzinfo is not None
    assert kw["original"] == _raw()  # the whole raw record is preserved for hashing


def test_normalise_rejects_missing_field() -> None:
    for key in ("instrument", "sample_ref", "element", "value", "unit", "time"):
        bad = _raw()
        del bad[key]
        with pytest.raises(ValueError, match=key):
            normalise_lab_result(bad)


def test_normalise_rejects_non_numeric_value() -> None:
    with pytest.raises(ValueError, match="numeric"):
        normalise_lab_result(_raw(value="high"))


def test_normalise_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        normalise_lab_result(_raw(time="2026-09-12T09:15:00"))


# -- write-once + hash immutability ---------------------------------------------------


def test_hash_is_order_independent() -> None:
    a = service.canonical_hash({"x": 1, "y": 2})
    b = service.canonical_hash({"y": 2, "x": 1})
    assert a == b and len(a) == 64


def test_record_is_idempotent_on_identical_replay(db_session: Session) -> None:
    kw = normalise_lab_result(_raw())
    row1, created1, conflict1 = service.record_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **kw
    )
    db_session.commit()
    row2, created2, conflict2 = service.record_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **normalise_lab_result(_raw())
    )
    assert created1 is True and created2 is False
    assert conflict1 is None and conflict2 is None
    assert row1.id == row2.id
    assert db_session.query(LaboratoryResult).count() == 1


def test_divergent_reimport_preserves_original_and_flags(db_session: Session) -> None:
    """A different original under the same id never overwrites — it raises a conflict flag."""
    kw = normalise_lab_result(_raw())
    row, created, _ = service.record_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **kw
    )
    db_session.commit()
    stored_hash = row.original_hash
    # Same (sample, element, time) → same id, but a tampered value/original.
    tampered = normalise_lab_result(_raw(value=9.99))
    row2, created2, conflict = service.record_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **tampered
    )
    db_session.commit()
    assert created2 is False
    assert row2.value == 3.42 and row2.original_hash == stored_hash  # original untouched
    assert conflict is not None and conflict.type == "lab_result_conflict"


# -- anomaly flagging (deterministic bounds) ------------------------------------------


def test_value_in_bounds_raises_no_anomaly(db_session: Session) -> None:
    row, created, alarms = service.ingest_result(
        db_session,
        site_id=SITE,
        created_by="lab",
        now=_NOW,
        bounds=_BOUNDS,
        **normalise_lab_result(_raw()),
    )
    db_session.commit()
    assert created is True and alarms == []


def test_value_out_of_bounds_flags_anomaly(db_session: Session) -> None:
    _, _, alarms = service.ingest_result(
        db_session,
        site_id=SITE,
        created_by="lab",
        now=_NOW,
        bounds=_BOUNDS,
        **normalise_lab_result(_raw(value=250.0)),
    )
    db_session.commit()
    assert len(alarms) == 1
    ev = alarms[0]
    assert ev.type == "lab_anomaly" and ev.severity == "warning" and ev.advisory is True
    assert ev.detail is not None and ev.detail["value"] == 250.0


def test_anomaly_is_deduped_on_replay(db_session: Session) -> None:
    for _ in range(3):
        service.ingest_result(
            db_session,
            site_id=SITE,
            created_by="lab",
            now=_NOW,
            bounds=_BOUNDS,
            **normalise_lab_result(_raw(value=250.0)),
        )
        db_session.commit()
    anomalies = [e for e in db_session.query(Event).all() if e.type == "lab_anomaly"]
    assert len(anomalies) == 1  # one result, one flag


def test_parse_bounds_tolerates_bad_config() -> None:
    assert service.parse_bounds("") == {}
    assert service.parse_bounds("not json") == {}
    assert service.parse_bounds('{"Au": [0, 100], "Ag": "bad"}') == {"Au": (0.0, 100.0)}


# -- append-only corrections ----------------------------------------------------------


def test_correction_is_appended_never_mutates(db_session: Session) -> None:
    row, _, _ = service.ingest_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **normalise_lab_result(_raw())
    )
    db_session.commit()
    corr = service.add_correction(
        db_session,
        site_id=SITE,
        result_id_=row.id,
        corrected_value=3.51,
        actor="chemist-jane",
        reason="dilution factor corrected",
        now=_NOW,
    )
    db_session.commit()
    assert corr is not None
    fresh = db_session.get(LaboratoryResult, row.id)
    assert fresh is not None and fresh.value == 3.42  # original untouched
    value, basis = service.effective_value(db_session, SITE, row.id)
    assert value == 3.51 and basis == "corrected"


def test_correction_history_is_retained(db_session: Session) -> None:
    row, _, _ = service.ingest_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **normalise_lab_result(_raw())
    )
    db_session.commit()
    for i, v in enumerate((3.5, 3.6, 3.7)):
        service.add_correction(
            db_session,
            site_id=SITE,
            result_id_=row.id,
            corrected_value=v,
            actor="jane",
            reason=f"pass {i}",
            now=datetime(2026, 9, 12, 11 + i, 0, tzinfo=UTC),
        )
        db_session.commit()
    history = service.list_corrections(db_session, SITE, row.id)
    assert [c.corrected_value for c in history] == [3.5, 3.6, 3.7]  # oldest first
    value, basis = service.effective_value(db_session, SITE, row.id)
    assert value == 3.7 and basis == "corrected"  # latest wins


def test_correction_on_unknown_result_returns_none(db_session: Session) -> None:
    assert (
        service.add_correction(
            db_session,
            site_id=SITE,
            result_id_="nope",
            corrected_value=1.0,
            actor="x",
            reason="y",
            now=_NOW,
        )
        is None
    )


def test_correction_is_site_scoped(db_session: Session) -> None:
    row, _, _ = service.ingest_result(
        db_session, site_id=SITE, created_by="lab", now=_NOW, **normalise_lab_result(_raw())
    )
    db_session.commit()
    # A correction attempted under a different site must not touch this result.
    assert (
        service.add_correction(
            db_session,
            site_id="other-site",
            result_id_=row.id,
            corrected_value=1.0,
            actor="x",
            reason="y",
            now=_NOW,
        )
        is None
    )


# -- CSV / batch ingest ---------------------------------------------------------------


def test_csv_parse_and_ingest(db_session: Session) -> None:
    raw = parse_spectraa_csv(SAMPLE_CSV, instrument="agilent-aa-2000")
    assert len(raw) == 2 and raw[0]["sample_ref"] == "S-2001"
    new, alarms = ingest_lab_results(db_session, SITE, raw, created_by="lab", bounds=_BOUNDS)
    assert len(new) == 2 and alarms == []


def test_batch_replay_is_idempotent(db_session: Session) -> None:
    new1, _ = ingest_lab_results(db_session, SITE, SAMPLE_RESULTS, created_by="lab")
    new2, _ = ingest_lab_results(db_session, SITE, SAMPLE_RESULTS, created_by="lab")
    assert len(new1) == 2 and len(new2) == 0
    assert db_session.query(LaboratoryResult).count() == 2


# -- contract registry ----------------------------------------------------------------


def test_contracts_registered() -> None:
    assert "laboratory.result.v1" in contracts.registered()
    assert "laboratory.correction.v1" in contracts.registered()


# -- API + RBAC -----------------------------------------------------------------------


def test_api_ingest_and_read_roundtrip(db_session: Session) -> None:
    client = make_client(db_session, SUPERVISOR)
    body = {
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-9001",
        "element": "Au",
        "value": 4.2,
        "unit": "g/t",
        "time": "2026-09-12T09:15:00+02:00",
    }
    r = client.post(f"/api/v1/sites/{SITE}/laboratory/results", json=body)
    assert r.status_code == 201, r.text
    result_id = r.json()["result"]["result_id"]
    assert r.json()["result"]["provenance"] == "measured"
    # read back with (empty) history + effective value
    g = client.get(f"/api/v1/sites/{SITE}/laboratory/results/{result_id}")
    assert g.status_code == 200
    assert g.json()["effective_value"] == {"value": 4.2, "basis": "measured"}
    # a raw original value is not exposed as a biometric/PII surface; only the hash is
    assert "original" not in g.json()["result"]
    assert len(g.json()["result"]["original_hash"]) == 64


def test_api_correction_flow(db_session: Session) -> None:
    client = make_client(db_session, SUPERVISOR)
    body = {
        "instrument": "agilent-aa-2000",
        "sample_ref": "S-9002",
        "element": "Au",
        "value": 4.2,
        "unit": "g/t",
        "time": "2026-09-12T09:15:00+02:00",
    }
    result_id = client.post(f"/api/v1/sites/{SITE}/laboratory/results", json=body).json()["result"][
        "result_id"
    ]
    c = client.post(
        f"/api/v1/sites/{SITE}/laboratory/results/{result_id}/corrections",
        json={"corrected_value": 4.5, "reason": "recalculated"},
    )
    assert c.status_code == 201, c.text
    g = client.get(f"/api/v1/sites/{SITE}/laboratory/results/{result_id}")
    assert g.json()["effective_value"] == {"value": 4.5, "basis": "corrected"}
    assert g.json()["corrections"][0]["actor"] == SUPERVISOR[0]


def test_api_csv_ingest(db_session: Session) -> None:
    client = make_client(db_session, SUPERVISOR)
    r = client.post(
        f"/api/v1/sites/{SITE}/laboratory/results/csv",
        json={"instrument": "agilent-aa-2000", "csv": SAMPLE_CSV},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 2


def test_api_viewer_cannot_ingest(db_session: Session) -> None:
    client = make_client(db_session, VIEWER)
    r = client.post(
        f"/api/v1/sites/{SITE}/laboratory/results",
        json={
            "instrument": "agilent-aa-2000",
            "sample_ref": "S-1",
            "element": "Au",
            "value": 1.0,
            "unit": "g/t",
            "time": "2026-09-12T09:15:00+02:00",
        },
    )
    assert r.status_code in (401, 403)


def test_api_rejects_malformed_value(db_session: Session) -> None:
    client = make_client(db_session, ADMIN)
    # value as a non-number is rejected by the request model (422) before the service.
    r = client.post(
        f"/api/v1/sites/{SITE}/laboratory/results",
        json={
            "instrument": "agilent-aa-2000",
            "sample_ref": "S-1",
            "element": "Au",
            "value": "high",
            "unit": "g/t",
            "time": "2026-09-12T09:15:00+02:00",
        },
    )
    assert r.status_code == 422


def test_api_get_unknown_result_404(db_session: Session) -> None:
    client = make_client(db_session, VIEWER)
    r = client.get(f"/api/v1/sites/{SITE}/laboratory/results/nope")
    assert r.status_code == 404
