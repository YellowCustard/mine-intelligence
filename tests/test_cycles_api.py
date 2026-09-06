"""HTTP coverage for the cycle-analytics endpoints (Phase 3 test-gap fill)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.storage.models import AssetMetrics, HaulCycle
from tests.conftest import VIEWER, make_client

_T0 = datetime(2026, 9, 5, 6, 0, 0, tzinfo=UTC)


def _seed_cycle(db: Session) -> None:
    db.add(
        HaulCycle(
            site_id="kn-zw-01",
            asset_id="HT-102",
            start_ts=_T0,
            end_ts=_T0,
            cycle_time_s=600.0,
            queue_s=120.0,
            load_s=120.0,
            haul_s=200.0,
            dump_s=80.0,
            return_s=80.0,
        )
    )
    db.add(
        AssetMetrics(
            site_id="kn-zw-01",
            asset_id="HT-102",
            bucket_start=_T0,
            bucket_end=_T0,
            distance_m=1000.0,
            moving_time_s=200.0,
            idle_time_s=100.0,
            max_speed_kph=40.0,
            mean_speed_kph=20.0,
            loads_completed=1,
        )
    )
    db.commit()


def test_get_cycles_returns_queue_pct(client: TestClient, db_session: Session) -> None:
    _seed_cycle(db_session)
    r = client.get("/sites/kn-zw-01/assets/HT-102/cycles")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["queue_pct"] == 20.0  # 120/600


def test_shift_summary_no_target_or_benchmark(client: TestClient, db_session: Session) -> None:
    _seed_cycle(db_session)
    r = client.get("/sites/kn-zw-01/shift-summary", params={"date": "2026-09-05", "shift": "day"})
    assert r.status_code == 200
    body = r.json()
    assert body["fleet"]["cycles"] == 1
    # Never a hardcoded target/benchmark — only observed segment percentages.
    text = r.text.lower()
    assert "target" not in text and "benchmark" not in text


def test_metrics_endpoint(client: TestClient, db_session: Session) -> None:
    _seed_cycle(db_session)
    r = client.get("/sites/kn-zw-01/metrics", params={"asset_id": "HT-102"})
    assert r.status_code == 200
    assert r.json()[0]["loads_completed"] == 1


def test_recompute_requires_admin(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    assert viewer.post("/sites/kn-zw-01/recompute").status_code == 403


def test_recompute_runs_and_audits(client: TestClient) -> None:
    r = client.post("/sites/kn-zw-01/recompute")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)  # counts of what was rebuilt
    audit = client.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "recompute" for a in audit)
