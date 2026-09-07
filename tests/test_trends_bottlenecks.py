"""Trends (per-shift series) and bottleneck observations (correlations, not causes)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.operations import bottlenecks, trends
from minemonitor.operations.shifts import resolve_shift_by_id
from minemonitor.storage.models import HaulCycle

_AT = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)  # day shift 2026-09-05
_SHIFT = "kn-zw-01:2026-09-05:day"


def _cycle(db: Session, asset: str, start: datetime, cycle_s: float, queue_s: float) -> None:
    db.add(
        HaulCycle(
            site_id="kn-zw-01",
            asset_id=asset,
            start_ts=start,
            end_ts=start,
            cycle_time_s=cycle_s,
            queue_s=queue_s,
            load_s=cycle_s * 0.1,
            haul_s=cycle_s * 0.3,
            dump_s=cycle_s * 0.1,
            return_s=cycle_s - queue_s - cycle_s * 0.5,
        )
    )


def test_trends_series_is_newest_first_and_sized(client: TestClient, db_session: Session) -> None:
    _cycle(db_session, "HT-102", _AT, 1000.0, 200.0)
    db_session.commit()
    body = client.get("/sites/kn-zw-01/trends", params={"shifts": 4, "at": _AT.isoformat()}).json()
    assert body["shifts"] == 4
    assert len(body["series"]) == 4
    # Newest first: each start strictly earlier than the previous.
    starts = [p["start"] for p in body["series"]]
    assert starts == sorted(starts, reverse=True)
    # The shift holding our cycle reports it; others are empty (null, not 0).
    withcycle = [p for p in body["series"] if p["cycles"] == 1]
    assert len(withcycle) == 1
    empties = [p for p in body["series"] if p["cycles"] == 0]
    assert all(p["mean_cycle_time_s"] is None for p in empties)


def test_recent_windows_helper(db_session: Session) -> None:
    ws = trends.recent_windows(db_session, "kn-zw-01", _AT, 3)
    assert len(ws) == 3
    assert ws[0].shift_id == _SHIFT  # newest is the shift containing _AT


def test_bottlenecks_flags_dominant_segment_and_disclaims_cause(
    client: TestClient, db_session: Session
) -> None:
    # Queue dominates the cycle → the segment-share observation should name it.
    _cycle(db_session, "HT-102", _AT, 1000.0, 700.0)
    db_session.commit()
    body = client.get("/sites/kn-zw-01/bottlenecks", params={"at": _AT.isoformat()}).json()
    kinds = {o["kind"] for o in body["observations"]}
    assert "segment_share" in kinds
    seg = next(o for o in body["observations"] if o["kind"] == "segment_share")
    assert seg["evidence"]["segment"] == "queue_s"
    assert seg["causal"] is False
    assert "not statements of cause" in body["note"]


def test_bottlenecks_flags_slow_asset(db_session: Session) -> None:
    # One truck far slower than the fleet mean → a slow_asset observation.
    _cycle(db_session, "HT-102", _AT, 600.0, 100.0)
    _cycle(db_session, "HT-200", _AT, 600.0, 100.0)
    _cycle(db_session, "HT-999", _AT, 1800.0, 100.0)  # 3x the others
    db_session.commit()
    window = resolve_shift_by_id(db_session, "kn-zw-01", _SHIFT)
    assert window is not None
    result = bottlenecks.compute_bottlenecks(db_session, "kn-zw-01", window)
    slow = [o for o in result["observations"] if o["kind"] == "slow_asset"]
    assert any(o["evidence"]["asset_id"] == "HT-999" for o in slow)


def test_bottlenecks_quiet_when_no_cycles(client: TestClient) -> None:
    body = client.get("/sites/kn-zw-01/bottlenecks", params={"at": _AT.isoformat()}).json()
    assert body["observations"] == []
