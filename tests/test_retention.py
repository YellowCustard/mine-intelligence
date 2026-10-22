"""Per-data-class retention deletion (M6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from minemonitor.retention import run_retention
from minemonitor.storage.models import (
    AuditLog,
    Base,
    DelayClassification,
    Event,
    Incident,
    IncidentNote,
    Position,
    ShiftHandover,
)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield s
    finally:
        s.close()


def _pos(s: Session, asset_id: str, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(
        Position(
            site_id="kn-zw-01",
            asset_id=asset_id,
            ts=ts,
            received_at=ts,
            lat=-17.8,
            lon=31.0,
            source="t",
        )
    )


def _event(s: Session, days_ago: float) -> None:
    ts = _NOW - timedelta(days=days_ago)
    s.add(
        Event(
            event_id=f"e{days_ago}",
            site_id="kn-zw-01",
            ts=ts,
            type="overspeed",
            severity="warning",
            source="t",
            summary="x",
            advisory=True,
            state="open",
        )
    )


def test_old_positions_deleted_recent_kept(session: Session) -> None:
    _pos(session, "A", days_ago=100)  # older than 90d
    _pos(session, "B", days_ago=10)  # recent
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["positions"] == 1
    remaining = session.execute(select(func.count()).select_from(Position)).scalar_one()
    assert remaining == 1  # the recent one survives


def test_zero_days_keeps_forever(session: Session) -> None:
    _pos(session, "A", days_ago=1000)
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=0, metrics_days=0, events_days=0)
    assert deleted["positions"] == 0
    assert session.execute(select(func.count()).select_from(Position)).scalar_one() == 1


def test_events_retention_and_audit_written(session: Session) -> None:
    _event(session, days_ago=400)
    _event(session, days_ago=5)
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["events"] == 1
    # A retention run is itself audited (accountable deletion, brief §4).
    audits = (
        session.execute(select(AuditLog).where(AuditLog.action == "retention.run")).scalars().all()
    )
    assert len(audits) == 1
    assert audits[0].detail["deleted"]["events"] == 1


def _audit(s: Session, days_ago: float) -> None:
    s.add(
        AuditLog(
            id=f"a{days_ago}",
            ts=_NOW - timedelta(days=days_ago),
            actor="system",
            action="test.event",
            entity_type="test",
        )
    )


def test_audit_log_is_pruned_but_run_record_survives(session: Session) -> None:
    _audit(session, days_ago=800)  # older than the 730d audit window
    _audit(session, days_ago=5)  # recent
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, audit_days=730
    )
    assert deleted["audit_log"] == 1  # only the old audit row
    # Survivors: the recent audit row + the run's own retention.run record.
    remaining = session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert remaining == 2


def test_audit_zero_days_keeps_forever(session: Session) -> None:
    _audit(session, days_ago=5000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=0, metrics_days=0, events_days=0, audit_days=0
    )
    assert deleted["audit_log"] == 0


# --- operational annotations -------------------------------------------------


def _incident(
    s: Session, iid: str, *, closed_days_ago: float | None, created_days_ago: float
) -> None:
    created = _NOW - timedelta(days=created_days_ago)
    closed = None if closed_days_ago is None else _NOW - timedelta(days=closed_days_ago)
    s.add(
        Incident(
            incident_id=iid,
            site_id="kn-zw-01",
            type="zone_breach",
            severity="critical",
            summary="x",
            state="closed" if closed else "open",
            created_by="sup",
            created_at=created,
            updated_at=created,
            closed_at=closed,
        )
    )
    s.add(
        IncidentNote(
            id=f"n-{iid}",
            incident_id=iid,
            site_id="kn-zw-01",
            ts=created,
            actor="sup",
            kind="note",
            text="did a thing",
        )
    )


def _delay(s: Session, did: str, end_days_ago: float) -> None:
    end = _NOW - timedelta(days=end_days_ago)
    s.add(
        DelayClassification(
            id=did,
            site_id="kn-zw-01",
            category="loader_unavailable",
            start_ts=end - timedelta(hours=1),
            end_ts=end,
            created_by="sup",
            created_at=end,
        )
    )


def _handover(s: Session, hid: str, created_days_ago: float) -> None:
    s.add(
        ShiftHandover(
            id=hid,
            site_id="kn-zw-01",
            shift_id="kn-zw-01:2026-01-01:day",
            summary={},
            outgoing_by="sup",
            created_at=_NOW - timedelta(days=created_days_ago),
        )
    )


def test_annotations_pruned_by_window(session: Session) -> None:
    # Closed-and-old incident -> deleted with its note; open incident of any age
    # and a recently-closed one survive.
    _incident(session, "old-closed", closed_days_ago=400, created_days_ago=410)
    _incident(session, "old-open", closed_days_ago=None, created_days_ago=999)
    _incident(session, "recent-closed", closed_days_ago=10, created_days_ago=20)
    _delay(session, "old-delay", end_days_ago=400)
    _delay(session, "recent-delay", end_days_ago=10)
    _handover(session, "old-h", created_days_ago=400)
    _handover(session, "recent-h", created_days_ago=10)
    session.commit()

    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=0,
        metrics_days=0,
        events_days=0,
        annotations_days=365,
    )
    assert deleted["incidents"] == 1 and deleted["incident_notes"] == 1
    assert deleted["delay_classifications"] == 1
    assert deleted["shift_handovers"] == 1

    surviving = {i for (i,) in session.execute(select(Incident.incident_id)).all()}
    assert surviving == {"old-open", "recent-closed"}  # open work never age-deleted
    assert session.execute(select(func.count()).select_from(IncidentNote)).scalar_one() == 2


def test_annotations_zero_days_keeps_forever(session: Session) -> None:
    _incident(session, "old-closed", closed_days_ago=9999, created_days_ago=9999)
    _delay(session, "old-delay", end_days_ago=9999)
    _handover(session, "old-h", created_days_ago=9999)
    session.commit()
    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=0,
        metrics_days=0,
        events_days=0,
        annotations_days=0,
    )
    assert deleted["incidents"] == 0
    assert deleted["delay_classifications"] == 0
    assert deleted["shift_handovers"] == 0


def test_access_quarantine_follows_events_window(session: Session) -> None:
    from minemonitor.storage.models import AccessQuarantine, Site

    session.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    session.flush()  # AccessQuarantine sorts before Site: write the site first
    for qid, days_ago in (("old", 400), ("new", 5)):
        seen = _NOW - timedelta(days=days_ago)
        session.add(
            AccessQuarantine(
                id=qid,
                site_id="kn-zw-01",
                source_system="dahua_gate",
                source_event_id=qid,
                gate_id="main-gate",
                decision="granted",
                claimed_ts=seen + timedelta(days=365),
                skew_s=365 * 86400.0,
                first_seen_at=seen,
            )
        )
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["access_quarantine"] == 1
    left = session.execute(select(AccessQuarantine.id)).scalars().all()
    assert left == ["new"]


def _passage(s: Session, pid: str, days_ago: float, operator_ref: str | None = "op-1") -> None:
    from minemonitor.storage.models import AccessEvent

    ts = _NOW - timedelta(days=days_ago)
    s.add(
        AccessEvent(
            id=pid,
            site_id="kn-zw-01",
            ts=ts,
            source_system="dahua_gate",
            gate_id="main-gate",
            operator_ref=operator_ref,
            decision="granted",
            search_selected=True,
            search_completed=True,
            metal_detected=False,
            created_at=ts,
        )
    )


def test_gate_passages_follow_their_own_window(session: Session) -> None:
    """Passages are personal data: deleted past ``access_days``, not kept forever."""
    from minemonitor.storage.models import AccessEvent, Site

    session.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    session.flush()  # AccessEvent sorts before Site: write the site first
    _passage(session, "old", 400)
    _passage(session, "new", 5)
    session.commit()
    deleted = run_retention(
        session,
        now=_NOW,
        positions_days=90,
        metrics_days=365,
        events_days=0,
        access_days=365,
    )
    assert deleted["access_events"] == 1
    assert session.execute(select(AccessEvent.id)).scalars().all() == ["new"]
    run = session.execute(select(AuditLog).where(AuditLog.action == "retention.run")).scalar_one()
    assert run.detail["policy_days"]["access"] == 365


def test_gate_passages_zero_days_keeps_forever(session: Session) -> None:
    from minemonitor.storage.models import AccessEvent, Site

    session.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    session.flush()
    _passage(session, "ancient", 4000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, access_days=0
    )
    assert deleted["access_events"] == 0
    assert session.execute(select(func.count()).select_from(AccessEvent)).scalar_one() == 1


def test_gate_passage_window_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from minemonitor.config import Settings

    assert Settings().retain_access_days == 365
    monkeypatch.setenv("MM_RETAIN_ACCESS_DAYS", "90")
    assert Settings().retain_access_days == 90


# --- operational records, equipment readings, vendor vision events ------------------------
# These tables had no retention class: they were kept forever. Each now has a window.


def _site(s: Session) -> None:
    from minemonitor.storage.models import Site

    s.add(Site(site_id="kn-zw-01", name="T", timezone="Africa/Harare"))
    s.flush()  # most models sort before Site: write the site first


def _seed_records(s: Session, tag: str, days_ago: float) -> None:
    """One row of every operational-record table, ``days_ago`` old, ids suffixed ``tag``."""
    from minemonitor.storage.models import (
        DispatchAssignment,
        DispatchJob,
        FuelTank,
        FuelTankReading,
        FuelTransaction,
        LaboratoryCorrection,
        LaboratoryResult,
        MaterialMeasurement,
        WeighTicket,
        WorkOrder,
    )

    ts = _NOW - timedelta(days=days_ago)
    s.add(FuelTank(tank_id=f"tank-{tag}", site_id="kn-zw-01", name="T1", created_at=ts))
    s.add(
        DispatchJob(
            id=f"job-{tag}",
            site_id="kn-zw-01",
            status="complete",
            created_by="sup",
            created_at=ts,
        )
    )
    s.add(
        LaboratoryResult(
            id=f"lab-{tag}",
            site_id="kn-zw-01",
            instrument="spectraa",
            sample_ref="S-1",
            ts=ts,
            element="Au",
            value=1.2,
            unit="g/t",
            original={"raw": 1.2},
            original_hash="h",
            created_at=ts,
            created_by="lab",
        )
    )
    s.flush()  # parents first: their children's class names sort before them
    s.add(
        WeighTicket(
            id=f"wt-{tag}",
            site_id="kn-zw-01",
            ticket_no=f"T-{tag}",
            ts=ts,
            gross_kg=40000.0,
            tare_kg=15000.0,
            net_kg=25000.0,
            operator_ref="op-1",
            created_at=ts,
            created_by="clerk",
        )
    )
    s.add(
        FuelTransaction(
            id=f"ft-{tag}",
            site_id="kn-zw-01",
            ts=ts,
            litres=300.0,
            created_at=ts,
            created_by="clerk",
        )
    )
    s.add(
        FuelTankReading(
            id=f"ftr-{tag}",
            site_id="kn-zw-01",
            tank_id=f"tank-{tag}",
            ts=ts,
            level_l=9000.0,
            created_at=ts,
        )
    )
    # A correction made recently to an old result still goes with its result.
    s.add(
        LaboratoryCorrection(
            id=f"labc-{tag}",
            site_id="kn-zw-01",
            result_id=f"lab-{tag}",
            corrected_value=1.3,
            actor="chemist",
            reason="recalibrated",
            ts=_NOW,
            created_at=_NOW,
        )
    )
    s.add(
        MaterialMeasurement(
            id=f"mm-{tag}",
            site_id="kn-zw-01",
            stage="mill",
            ts=ts,
            quantity=120.0,
            unit="t",
            created_at=ts,
            created_by="clerk",
        )
    )
    s.add(
        DispatchAssignment(
            id=f"da-{tag}",
            site_id="kn-zw-01",
            job_id=f"job-{tag}",
            asset_id="HT-102",
            state="released",
            recommended_at=ts,
        )
    )
    s.add(
        WorkOrder(
            id=f"wo-{tag}",
            site_id="kn-zw-01",
            asset_id="HT-102",
            component="machine",
            type="repair",
            status="done",
            opened_at=ts,
            closed_at=ts,
            created_by="fitter",
            created_at=ts,
        )
    )


def _ids(s: Session, model: Any) -> list[str]:
    return sorted(s.execute(select(model.id)).scalars().all())


def test_operational_records_follow_the_records_window(session: Session) -> None:
    """Weigh tickets, fuel, lab, material, dispatch and work orders are deleted past the window."""
    from minemonitor.storage.models import (
        DispatchAssignment,
        DispatchJob,
        FuelTankReading,
        FuelTransaction,
        LaboratoryCorrection,
        LaboratoryResult,
        MaterialMeasurement,
        WeighTicket,
        WorkOrder,
    )

    _site(session)
    _seed_records(session, "old", 2600)
    _seed_records(session, "new", 30)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, records_days=2555
    )
    for key in (
        "weigh_tickets",
        "fuel_transactions",
        "fuel_tank_readings",
        "laboratory_results",
        "laboratory_corrections",
        "material_measurements",
        "dispatch_jobs",
        "dispatch_assignments",
        "work_orders",
    ):
        assert deleted[key] == 1, key
    assert _ids(session, WeighTicket) == ["wt-new"]
    assert _ids(session, FuelTransaction) == ["ft-new"]
    assert _ids(session, FuelTankReading) == ["ftr-new"]
    assert _ids(session, LaboratoryResult) == ["lab-new"]
    assert _ids(session, LaboratoryCorrection) == ["labc-new"]
    assert _ids(session, MaterialMeasurement) == ["mm-new"]
    assert _ids(session, DispatchJob) == ["job-new"]
    assert _ids(session, DispatchAssignment) == ["da-new"]
    assert _ids(session, WorkOrder) == ["wo-new"]
    run = session.execute(select(AuditLog).where(AuditLog.action == "retention.run")).scalar_one()
    assert run.detail["policy_days"]["records"] == 2555


def test_records_zero_days_keeps_forever(session: Session) -> None:
    from minemonitor.storage.models import WeighTicket, WorkOrder

    _site(session)
    _seed_records(session, "ancient", 9000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, records_days=0
    )
    assert deleted["weigh_tickets"] == 0
    assert deleted["work_orders"] == 0
    assert _ids(session, WeighTicket) == ["wt-ancient"]
    assert _ids(session, WorkOrder) == ["wo-ancient"]


def test_live_dispatch_jobs_and_work_orders_are_never_age_deleted(session: Session) -> None:
    """An open job or work order is live work; the latest service is the health baseline."""
    from minemonitor.storage.models import DispatchAssignment, DispatchJob, WorkOrder

    _site(session)
    old = _NOW - timedelta(days=3000)
    session.add(
        DispatchJob(
            id="job-open", site_id="kn-zw-01", status="open", created_by="s", created_at=old
        )
    )
    session.flush()
    session.add(
        DispatchAssignment(
            id="da-open",
            site_id="kn-zw-01",
            job_id="job-open",
            asset_id="HT-102",
            state="active",
            recommended_at=old,
        )
    )

    def _wo(wid: str, *, type: str, status: str, component: str, days_ago: float) -> None:
        ts = _NOW - timedelta(days=days_ago)
        session.add(
            WorkOrder(
                id=wid,
                site_id="kn-zw-01",
                asset_id="HT-102",
                component=component,
                type=type,
                status=status,
                opened_at=ts,
                closed_at=ts if status == "done" else None,
                created_by="fitter",
                created_at=ts,
            )
        )

    _wo("wo-open", type="repair", status="open", component="machine", days_ago=3000)
    _wo("wo-svc-superseded", type="service", status="done", component="engine", days_ago=3100)
    _wo("wo-svc-latest-old", type="service", status="done", component="engine", days_ago=3000)
    _wo("wo-svc-only", type="service", status="done", component="gearbox", days_ago=3000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, records_days=2555
    )
    assert deleted["dispatch_jobs"] == 0
    assert _ids(session, DispatchJob) == ["job-open"]
    assert _ids(session, DispatchAssignment) == ["da-open"]
    assert deleted["work_orders"] == 1
    assert _ids(session, WorkOrder) == ["wo-open", "wo-svc-latest-old", "wo-svc-only"]


def _gen(s: Session, asset_id: str, days_ago: float) -> None:
    from minemonitor.storage.models import GeneratorReading

    ts = _NOW - timedelta(days=days_ago)
    s.add(
        GeneratorReading(
            id=f"gen-{asset_id}-{days_ago}",
            site_id="kn-zw-01",
            asset_id=asset_id,
            ts=ts,
            engine_hours=1000.0 - days_ago,
            running=True,
            created_at=ts,
        )
    )


def test_generator_readings_follow_the_readings_window(session: Session) -> None:
    """High-volume readings go past the window; each genset's latest reading is kept."""
    from minemonitor.storage.models import GeneratorReading

    _site(session)
    _gen(session, "GEN-1", 200)
    _gen(session, "GEN-1", 100)
    _gen(session, "GEN-1", 5)
    _gen(session, "GEN-2", 300)  # silent genset: its last reading is its engine hours
    _gen(session, "GEN-2", 200)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, readings_days=90
    )
    assert deleted["generator_readings"] == 3
    assert _ids(session, GeneratorReading) == ["gen-GEN-1-5", "gen-GEN-2-200"]
    run = session.execute(select(AuditLog).where(AuditLog.action == "retention.run")).scalar_one()
    assert run.detail["policy_days"]["readings"] == 90


def test_generator_readings_zero_days_keeps_forever(session: Session) -> None:
    from minemonitor.storage.models import GeneratorReading

    _site(session)
    _gen(session, "GEN-1", 5000)
    _gen(session, "GEN-1", 4000)
    session.commit()
    deleted = run_retention(
        session, now=_NOW, positions_days=90, metrics_days=365, events_days=365, readings_days=0
    )
    assert deleted["generator_readings"] == 0
    assert len(_ids(session, GeneratorReading)) == 2


def _vendor_event(s: Session, vid: str, days_ago: float) -> None:
    from minemonitor.storage.models import VisionVendorEvent

    ts = _NOW - timedelta(days=days_ago)
    s.add(
        VisionVendorEvent(
            id=vid,
            site_id="kn-zw-01",
            source_system="dahua_nvr",
            source_event_id=vid,
            vendor_type="CrossLineDetection",
            normalized_type="line_crossing",
            ts=ts,
            created_at=ts,
        )
    )


def test_vision_vendor_events_follow_the_events_window(session: Session) -> None:
    from minemonitor.storage.models import VisionVendorEvent

    _site(session)
    _vendor_event(session, "old", 400)
    _vendor_event(session, "new", 5)
    session.commit()
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=365)
    assert deleted["vision_vendor_events"] == 1
    assert _ids(session, VisionVendorEvent) == ["new"]
    deleted = run_retention(session, now=_NOW, positions_days=90, metrics_days=365, events_days=0)
    assert deleted["vision_vendor_events"] == 0


def test_new_windows_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from minemonitor.config import Settings

    assert Settings().retain_records_days == 2555
    assert Settings().retain_readings_days == 90
    monkeypatch.setenv("MM_RETAIN_RECORDS_DAYS", "3650")
    monkeypatch.setenv("MM_RETAIN_READINGS_DAYS", "0")
    assert Settings().retain_records_days == 3650
    assert Settings().retain_readings_days == 0


def test_run_from_config_applies_the_new_windows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minemonitor.config import get_settings
    from minemonitor.retention import run_from_config
    from minemonitor.storage.models import WeighTicket

    monkeypatch.setenv("MM_RETAIN_RECORDS_DAYS", "100")
    monkeypatch.setenv("MM_RETAIN_READINGS_DAYS", "50")
    get_settings.cache_clear()
    try:
        _site(session)
        _seed_records(session, "old", 200)
        _gen(session, "GEN-1", 60)
        _gen(session, "GEN-1", 1)
        session.commit()
        deleted = run_from_config(session, now=_NOW)
    finally:
        get_settings.cache_clear()
    assert deleted["weigh_tickets"] == 1 and _ids(session, WeighTicket) == []
    assert deleted["generator_readings"] == 1
