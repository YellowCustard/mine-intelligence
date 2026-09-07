"""Data-quality monitoring and the platform-vs-field system-health verdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.operations import systemhealth
from minemonitor.storage.models import Position

_NOW = datetime.now(UTC)


def _pos(db: Session, asset: str, ts: datetime, lat: float, lon: float, **kw: object) -> None:
    db.add(
        Position(
            site_id="kn-zw-01",
            asset_id=asset,
            ts=ts,
            received_at=kw.get("received_at", ts),
            lat=lat,
            lon=lon,
            speed_kph=kw.get("speed_kph", 10.0),
            ignition=True,
            hdop=kw.get("hdop", 0.9),
            satellites=kw.get("satellites", 11),
            source="test",
        )
    )


def test_data_quality_good_on_clean_recent_fixes(client: TestClient, db_session: Session) -> None:
    base = _NOW - timedelta(seconds=30)
    for i in range(5):
        _pos(db_session, "HT-102", base + timedelta(seconds=i), -17.8252, 31.0335 + i * 1e-4)
    db_session.commit()
    dq = client.get("/sites/kn-zw-01/data-quality").json()
    assert dq["confidence"] == "good"
    assert dq["issues"]["stale_feeds"] == 0


def test_data_quality_flags_impossible_move_and_missing_fields(
    client: TestClient, db_session: Session
) -> None:
    base = _NOW - timedelta(seconds=30)
    # Two fixes 1s apart but ~1 km apart → ~3600 kph, impossible; and no hdop/sats.
    _pos(db_session, "HT-102", base, -17.8252, 31.0335, hdop=None, satellites=None)
    _pos(
        db_session,
        "HT-102",
        base + timedelta(seconds=1),
        -17.8252,
        31.0425,
        hdop=None,
        satellites=None,
    )
    db_session.commit()
    dq = client.get("/sites/kn-zw-01/data-quality").json()
    assert dq["issues"]["impossible_moves"] >= 1
    assert dq["issues"]["missing_quality_fields"] >= 1


def test_data_quality_flags_stale_feed(client: TestClient, db_session: Session) -> None:
    _pos(db_session, "HT-102", _NOW - timedelta(hours=3), -17.8252, 31.0335)
    db_session.commit()
    dq = client.get("/sites/kn-zw-01/data-quality").json()
    assert dq["issues"]["stale_feeds"] == 1
    assert "HT-102" in dq["stale_feed_assets"]


def test_data_quality_flags_late_backfilled_fix(client: TestClient, db_session: Session) -> None:
    ts = _NOW - timedelta(seconds=20)
    _pos(db_session, "HT-102", ts, -17.8252, 31.0335, received_at=ts + timedelta(minutes=10))
    db_session.commit()
    dq = client.get("/sites/kn-zw-01/data-quality").json()
    assert dq["issues"]["late_or_backfilled_fixes"] >= 1


def test_system_health_healthy_when_app_up_and_ingest_flowing(db_session: Session) -> None:
    _pos(db_session, "HT-102", _NOW - timedelta(seconds=10), -17.8252, 31.0335)
    db_session.commit()
    out = systemhealth.compute_system_health(
        db_session, "kn-zw-01", _NOW, offline_after_s=600, ingestor_fresh=True, mqtt_ok=True
    )
    assert out["verdict"] == "healthy"
    assert out["field_plane"]["ingest_flowing"] is True


def test_system_health_blames_platform_when_app_down(db_session: Session) -> None:
    _pos(db_session, "HT-102", _NOW - timedelta(seconds=10), -17.8252, 31.0335)
    db_session.commit()
    out = systemhealth.compute_system_health(
        db_session, "kn-zw-01", _NOW, offline_after_s=600, ingestor_fresh=False, mqtt_ok=True
    )
    assert out["verdict"] == "platform_degraded"


def test_system_health_blames_field_when_app_up_but_feeds_silent(db_session: Session) -> None:
    # App healthy, but the only asset's last fix is hours old → field problem.
    _pos(db_session, "HT-102", _NOW - timedelta(hours=3), -17.8252, 31.0335)
    db_session.commit()
    out = systemhealth.compute_system_health(
        db_session, "kn-zw-01", _NOW, offline_after_s=600, ingestor_fresh=True, mqtt_ok=True
    )
    assert out["verdict"] == "field_degraded"
    assert out["field_plane"]["silent_feeds"] == 1


def test_system_health_endpoint(client: TestClient) -> None:
    out = client.get("/sites/kn-zw-01/system-health").json()
    assert out["verdict"] in ("healthy", "platform_degraded", "field_degraded")
    assert "app_plane" in out and "field_plane" in out
