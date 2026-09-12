"""Platform foundation (Phase 1): contract registry, event bus, metrics, /api/v1."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from minemonitor.contracts import EventV1
from minemonitor.platform import contracts
from minemonitor.platform.bus import EventBus
from minemonitor.platform.metrics import Metrics
from tests.conftest import ADMIN, VIEWER, make_client


def _event(event_id: str = "e1") -> EventV1:
    return EventV1(
        event_id=event_id,
        site_id="kn-zw-01",
        ts=datetime(2026, 9, 5, 11, 42, tzinfo=UTC),
        type="zone_breach",
        severity="critical",
        source="gnss_geofence",
        summary="test",
    )


# --- contract registry -------------------------------------------------------


def test_registry_knows_the_existing_contracts() -> None:
    reg = contracts.registered()
    assert {"asset.position.v1", "event.v1", "asset.metrics.v1"} <= set(reg)


def test_validate_roundtrips_and_rejects_unknown() -> None:
    ev = contracts.validate("event.v1", _event().model_dump(by_alias=True))
    assert isinstance(ev, EventV1) and ev.event_id == "e1"
    with pytest.raises(KeyError):
        contracts.validate("nope.v1", {})


def test_register_is_version_immutable() -> None:
    class _Other(BaseModel):
        pass

    # Re-registering the same model is fine; rebinding to a different one is refused.
    contracts.register("event.v1", EventV1)
    with pytest.raises(ValueError, match="already registered"):
        contracts.register("event.v1", _Other)


# --- metrics -----------------------------------------------------------------


def test_metrics_counts_and_snapshots() -> None:
    m = Metrics()
    m.incr("a")
    m.incr("a", 2)
    m.incr("b")
    assert m.get("a") == 3
    assert m.snapshot() == {"a": 3, "b": 1}


# --- event bus ---------------------------------------------------------------


def test_bus_delivers_to_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("event.v1", lambda e: seen.append(e.event_id))
    delivered = bus.publish(_event("x"))
    assert delivered == 1 and seen == ["x"]


def test_bus_isolates_a_failing_subscriber() -> None:
    bus = EventBus()
    got: list[str] = []

    def bad(_e: BaseModel) -> None:
        raise RuntimeError("boom")

    bus.subscribe("event.v1", bad)
    bus.subscribe("event.v1", lambda e: got.append(e.event_id))
    # One handler raises; the other still runs; the publisher does not.
    delivered = bus.publish(_event("y"))
    assert delivered == 1 and got == ["y"]  # only the good handler counted


def test_bus_no_subscribers_is_a_noop() -> None:
    assert EventBus().publish(_event()) == 0


# --- /api/v1 dual-mount + platform surface -----------------------------------


def test_legacy_and_v1_paths_both_serve(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    # A representative existing endpoint works at both its historical path and /api/v1.
    assert c.get("/me").status_code == 200
    assert c.get("/api/v1/me").status_code == 200


def test_platform_contracts_endpoint(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    body = c.get("/api/v1/platform/contracts").json()
    assert "event.v1" in body["contracts"]


def test_platform_metrics_is_admin_only(db_session: Session) -> None:
    assert make_client(db_session, VIEWER).get("/api/v1/platform/metrics").status_code == 403
    r = make_client(db_session, ADMIN).get("/api/v1/platform/metrics")
    assert r.status_code == 200 and "metrics" in r.json()
