"""Health probes: liveness vs full-system, MQTT and ingestor-heartbeat reporting (Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor import heartbeat
from minemonitor.storage.models import ServiceHeartbeat
from tests.conftest import make_client


def test_healthz_liveness_is_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_health_degraded_when_mqtt_and_ingestor_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("minemonitor.api.routers.health._mqtt_reachable", lambda *a, **k: False)
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["mqtt"] == "unavailable"
    assert body["ingestor"] == "stale"  # no heartbeat written


def test_health_ok_when_all_up(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("minemonitor.api.routers.health._mqtt_reachable", lambda *a, **k: True)
    heartbeat.beat(db_session, heartbeat.INGESTOR)
    db_session.commit()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "mqtt": "ok", "ingestor": "ok"}


def test_health_flags_stale_ingestor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("minemonitor.api.routers.health._mqtt_reachable", lambda *a, **k: True)
    old = datetime.now(UTC) - timedelta(seconds=10_000)
    db_session.add(ServiceHeartbeat(service=heartbeat.INGESTOR, ts=old))
    db_session.commit()
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["ingestor"] == "stale"


def test_healthz_is_public(db_session: Session) -> None:
    anon = make_client(db_session, None)
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/health").status_code in (200, 503)  # reachable without auth


def test_healthcheck_cli_exit_codes(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import sessionmaker

    from minemonitor import healthcheck

    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr(healthcheck, "get_session_factory", lambda: factory)
    # No heartbeat yet -> unhealthy.
    assert healthcheck.main() == 1
    # After a fresh beat -> healthy.
    heartbeat.beat(db_session, heartbeat.INGESTOR)
    db_session.commit()
    assert healthcheck.main() == 0


def test_heartbeat_helpers(db_session: Session) -> None:
    assert heartbeat.age_s(db_session, heartbeat.INGESTOR) is None
    heartbeat.beat(db_session, heartbeat.INGESTOR)
    db_session.commit()
    assert heartbeat.is_fresh(db_session, heartbeat.INGESTOR, stale_s=180)
    age = heartbeat.age_s(db_session, heartbeat.INGESTOR)
    assert age is not None and age < 5
