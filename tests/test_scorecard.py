"""Shift scorecard: observed/derived aggregation over a shift window."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.events.repository import new_event_id, persist_event
from minemonitor.operations import delays, incidents
from minemonitor.storage.models import AssetMetrics, HaulCycle

# 12:00 local (UTC+2) on 2026-09-05 → inside the default day shift (04:00–16:00Z).
_AT = "2026-09-05T10:00:00Z"
_IN_WINDOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)


def _seed_shift(db: Session) -> None:
    db.add(
        HaulCycle(
            site_id="kn-zw-01",
            asset_id="HT-102",
            start_ts=_IN_WINDOW,
            end_ts=datetime(2026, 9, 5, 10, 20, 0, tzinfo=UTC),
            cycle_time_s=1000.0,
            queue_s=200.0,
            load_s=100.0,
            haul_s=400.0,
            dump_s=100.0,
            return_s=200.0,
        )
    )
    db.add(
        AssetMetrics(
            site_id="kn-zw-01",
            asset_id="HT-102",
            bucket_start=datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC),
            bucket_end=datetime(2026, 9, 5, 9, 5, 0, tzinfo=UTC),
            moving_time_s=300.0,
            idle_time_s=100.0,
        )
    )
    persist_event(
        db,
        EventV1(
            event_id=new_event_id(),
            site_id="kn-zw-01",
            ts=_IN_WINDOW,
            type="zone_breach",
            severity="critical",
            asset_id="LV-07",
            source="gnss_geofence",
            summary="unauthorised entry",
        ),
    )
    delays.create_classification(
        db,
        site_id="kn-zw-01",
        category="maintenance",
        start_ts=datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC),
        end_ts=datetime(2026, 9, 5, 8, 30, 0, tzinfo=UTC),
        actor="sup",
    )
    incidents.create_incident(
        db,
        site_id="kn-zw-01",
        summary="loader down",
        type_="operational",
        severity="warning",
        actor="sup",
        now=_IN_WINDOW,
    )
    db.commit()


def test_scorecard_aggregates_observed_numbers(client: TestClient, db_session: Session) -> None:
    _seed_shift(db_session)
    card = client.get(f"/sites/kn-zw-01/scorecard?at={_AT}").json()

    assert card["shift"]["name"] == "day"
    assert card["cycles"]["count"] == 1
    assert card["cycles"]["queue_pct"] == 20.0  # 200 / 1000
    assert card["utilisation"]["utilisation_pct"] == 75.0  # 300 / 400
    assert card["utilisation"]["basis"] == "derived"
    assert card["safety_events"]["total"] == 1
    assert card["safety_events"]["by_severity"]["critical"] == 1
    assert card["delays"]["classified_total_s"] == 1800.0  # 30 min
    assert card["delays"]["by_category"]["maintenance"] == 1800.0
    assert card["incidents"]["open_now"] == 1
    assert card["incidents"]["opened_this_shift"] == 1


def test_scorecard_offers_previous_shift_comparison(client: TestClient) -> None:
    card = client.get(f"/sites/kn-zw-01/scorecard?at={_AT}").json()
    assert card["comparison"] is not None
    assert card["comparison"]["previous_shift_id"].startswith("kn-zw-01:")
    # No cycles either shift → a queue delta cannot be computed, and we say so
    # rather than inventing a number.
    assert card["comparison"]["queue_pct_delta"] is None


def test_delay_clipped_to_window(client: TestClient, db_session: Session) -> None:
    # A delay straddling the shift start (03:30–04:30Z) counts only its in-window half.
    delays.create_classification(
        db_session,
        site_id="kn-zw-01",
        category="breakdown",
        start_ts=datetime(2026, 9, 5, 3, 30, 0, tzinfo=UTC),
        end_ts=datetime(2026, 9, 5, 4, 30, 0, tzinfo=UTC),
        actor="sup",
    )
    db_session.commit()
    card = client.get(f"/sites/kn-zw-01/scorecard?at={_AT}").json()
    # Window starts 04:00Z, so only 04:00–04:30 (1800s) is inside.
    assert card["delays"]["by_category"]["breakdown"] == 1800.0


def test_scorecard_empty_shift_is_honest(client: TestClient) -> None:
    card = client.get(f"/sites/kn-zw-01/scorecard?at={_AT}").json()
    assert card["cycles"]["count"] == 0
    assert card["cycles"]["mean_cycle_time_s"] is None  # not zero — nothing observed
    assert card["utilisation"]["utilisation_pct"] is None
