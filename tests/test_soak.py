"""The soak harness's own parts (the PostgreSQL run itself is ``test_soak_postgres.py``)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from minemonitor import soak

T0 = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


def test_a_fleet_of_fifty_is_the_simulator_fleet_copied_and_renamed_apart() -> None:
    fleet = soak.Fleet("s", 50, seed=1, start=T0, tick_s=1.0)
    assert len(fleet.classes) == 50
    # Five full copies of the nine-machine fleet, then the first five of a sixth (its trucks).
    assert Counter(fleet.classes.values())["haul_truck"] == 30
    assert {"HT-101-0", "LV-07-4", "HT-105-5"} <= set(fleet.classes)
    assert "EX-01-5" not in fleet.classes
    batch = fleet.step()
    assert sorted(p.asset_id for p in batch) == sorted(fleet.classes)
    assert {p.ts for p in batch} == {T0} and {p.site_id for p in batch} == {"s"}
    assert fleet.now == T0 + timedelta(seconds=1)


def test_the_fleet_steps_at_the_history_rate_then_live() -> None:
    fleet = soak.Fleet("s", 9, seed=1, start=T0, tick_s=300.0)
    fleet.step()
    fleet.set_tick(1.0)
    fleet.step()
    assert fleet.now == T0 + timedelta(seconds=301)


def test_a_live_phase_too_short_to_time_a_tick_is_refused() -> None:
    with pytest.raises(ValueError, match="too short"):
        soak.SoakConfig(live_s=60, tick_every_s=60)  # the one tick measures memory
    with pytest.raises(ValueError, match="too short"):
        soak.SoakConfig(live_s=120, tick_every_s=60, explain=True)
    soak.SoakConfig(live_s=180, tick_every_s=60, explain=True)


def test_it_refuses_anything_but_postgresql() -> None:
    engine = create_engine("sqlite://")
    with pytest.raises(soak.RefusedError, match="PostgreSQL"):
        soak.run(engine, soak.SoakConfig())


def test_percentiles_by_nearest_rank() -> None:
    assert soak._percentiles([]) == {"n": 0}
    assert soak._percentiles([3.0]) == {"n": 1, "p50": 3.0, "p95": 3.0, "max": 3.0}
    xs = [float(i) for i in range(1, 101)]
    assert soak._percentiles(xs) == {"n": 100, "p50": 51.0, "p95": 95.0, "max": 100.0}


def test_the_report_lists_the_ten_heaviest_queries_of_each_kind() -> None:
    stmts = {f"SELECT\n  {i}": i for i in range(12)}
    report = soak.Report(config={}, budgets={}, statements={"tick": stmts, "state": {}})
    out = report.to_json()
    assert [s["blocks"] for s in out["statements"]["tick"]] == list(range(11, 1, -1))
    assert out["statements"]["tick"][0]["sql"] == "SELECT 11"
    assert out["statements"]["state"] == []


def test_each_budget_is_reported_when_broken() -> None:
    report = soak.Report(
        config={},
        budgets={},
        tick={"p95": 7.0},
        state={"p95": 0.1},
        ingest={"headroom": 1.5, "statements_per_fix": 13.0},
        memory={"tick_mib": 1.0, "state_mib": 1.0},
    )
    assert soak._over_budget(report, soak.Budgets()) == [
        "tick p95 s 7.000 over budget 6.0",
        "statements per fix 13.000 over budget 9.0",
        "ingest headroom 1.50 under budget 2.0",
    ]
