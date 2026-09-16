"""Access-control integration (FP-07): normalisation, the no-biometrics boundary,
authorisation rules, unauthorised-entry alarms, idempotency, API + RBAC."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from minemonitor.access import service
from minemonitor.events.repository import list_events
from minemonitor.ingest.adapters.access_sim import (
    SAMPLE_EVENTS,
    ingest_access_events,
    normalise_access_event,
)
from minemonitor.platform import contracts
from minemonitor.storage.models import Operator, ShiftDefinition
from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client

SITE = "kn-zw-01"
# On the default 24h-tiling shifts, this instant is on-shift; used unless a test narrows shifts.
_TS = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 12, 8, 5, tzinfo=UTC)


def _operator(db: Session, oid: str = "OP-001", *, suspended: bool = False, inducted: bool = True):
    db.add(
        Operator(
            operator_id=oid,
            site_id=SITE,
            display_name="Test Operator",
            suspended=suspended,
            inducted=inducted,
            created_at=_NOW,
        )
    )
    db.commit()


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "g-1",
        "source_system": "alhua_gate",
        "gate_id": "main-gate",
        "time": "2026-09-12T10:00:07+02:00",
        "decision": "granted",
        "operator_ref": "OP-001",
        "credential_ref": "face-evt-88",
    }
    base.update(over)
    return base


# -- normalisation & the no-biometrics boundary ---------------------------------------


def test_normalise_maps_fields() -> None:
    kw = normalise_access_event(_raw())
    assert kw["source_system"] == "alhua_gate" and kw["gate_id"] == "main-gate"
    assert kw["source_event_id"] == "g-1" and kw["decision"] == "granted"
    assert kw["ts"].tzinfo is not None and kw["operator_ref"] == "OP-001"


def test_biometric_payload_is_refused() -> None:
    for key in ("face_template", "image", "embedding", "photo"):
        with pytest.raises(ValueError, match="biometric"):
            normalise_access_event(_raw(**{key: "<data>"}))


def test_missing_field_and_naive_timestamp_rejected() -> None:
    bad = _raw()
    del bad["gate_id"]
    with pytest.raises(ValueError, match="missing required field"):
        normalise_access_event(bad)
    with pytest.raises(ValueError, match="timezone-aware"):
        normalise_access_event(_raw(time="2026-09-12T10:00:07"))


# -- authorisation rules --------------------------------------------------------------


def test_authorize_unknown_without_registered_operator(db_session: Session) -> None:
    assert service.authorize(db_session, SITE, None, _TS)[0] == "unknown"
    assert service.authorize(db_session, SITE, "GHOST", _TS)[0] == "unknown"


def test_authorize_denies_suspended_and_non_inducted(db_session: Session) -> None:
    _operator(db_session, "OP-SUS", suspended=True)
    _operator(db_session, "OP-NI", inducted=False)
    assert service.authorize(db_session, SITE, "OP-SUS", _TS) == ("denied", "operator suspended")
    assert service.authorize(db_session, SITE, "OP-NI", _TS) == ("denied", "operator not inducted")


def test_authorize_grants_valid_operator_on_shift(db_session: Session) -> None:
    _operator(db_session, "OP-OK")
    assert service.authorize(db_session, SITE, "OP-OK", _TS) == ("granted", None)


def test_authorize_denies_off_shift(db_session: Session) -> None:
    # A single narrow day shift (06:00–14:00 local = 04:00–12:00 UTC) → 18:00 UTC is off-shift.
    db_session.add(ShiftDefinition(site_id=SITE, name="day", start_hour_local=6, duration_hours=8))
    _operator(db_session, "OP-OK")
    off = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
    assert service.authorize(db_session, SITE, "OP-OK", off) == ("denied", "off-shift")


# -- ingest: record + unauthorised-entry alarm ----------------------------------------


def _ingest(db: Session, **over: Any):
    return ingest_access_events(db, SITE, [_raw(**over)], now=_NOW)


def test_granted_entry_for_suspended_operator_raises_critical_alarm(db_session: Session) -> None:
    _operator(db_session, "OP-001", suspended=True)
    new, alarms = _ingest(db_session)  # gate granted, but operator is suspended
    assert len(new) == 1 and len(alarms) == 1
    a = alarms[0]
    assert a.type == "access_denied" and a.severity == "critical" and a.advisory is True
    assert "suspended" in a.summary and a.zone_id is None
    # The alarm is in the unified queue.
    assert any(e.type == "access_denied" for e in list_events(db_session, SITE))


def test_gate_denial_does_not_alarm(db_session: Session) -> None:
    _operator(db_session, "OP-001", suspended=True)
    # The gate already denied this suspended operator → Mine Monitor records, does not alarm.
    new, alarms = _ingest(db_session, decision="denied")
    assert len(new) == 1 and alarms == []


def test_authorised_entry_does_not_alarm(db_session: Session) -> None:
    _operator(db_session, "OP-001")  # valid, on-shift (default 24h coverage)
    new, alarms = _ingest(db_session)
    assert len(new) == 1 and alarms == []


def test_replay_is_idempotent_and_never_re_alarms(db_session: Session) -> None:
    _operator(db_session, "OP-001", suspended=True)
    first_new, first_alarms = _ingest(db_session)
    assert len(first_new) == 1 and len(first_alarms) == 1
    second_new, second_alarms = _ingest(db_session)  # same source event id
    assert second_new == [] and second_alarms == []
    assert len(list_events(db_session, SITE)) == 1  # not double-alarmed


def test_sample_events_ingest(db_session: Session) -> None:
    new, _alarms = ingest_access_events(db_session, SITE, SAMPLE_EVENTS, now=_NOW)
    assert len(new) == len(SAMPLE_EVENTS)
    assert ingest_access_events(db_session, SITE, SAMPLE_EVENTS, now=_NOW) == ([], [])


# -- API + RBAC -----------------------------------------------------------------------


def test_contract_registered() -> None:
    assert "access.event.v1" in contracts.registered()


def test_api_ingest_and_read_and_status(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    admin = make_client(db_session, ADMIN)
    # Admin suspends the operator.
    r = admin.post(f"/sites/{SITE}/operators/OP-001/access-status", json={"suspended": True})
    assert r.status_code == 200 and r.json()["suspended"] is True

    sup = make_client(db_session, SUPERVISOR)
    body = _raw(id="api-1")
    res = sup.post(f"/sites/{SITE}/access/events", json=body)
    assert res.status_code == 201
    assert res.json()["created"] is True and res.json()["alarm_raised"] is True

    # Viewer can read the access events at both path styles.
    v = make_client(db_session, VIEWER)
    assert v.get(f"/sites/{SITE}/access/events").status_code == 200
    assert v.get(f"/api/v1/sites/{SITE}/access/events").json()[0]["gate_id"] == "main-gate"


def test_api_rbac_and_biometric_rejection(db_session: Session) -> None:
    v = make_client(db_session, VIEWER)
    # Viewer cannot ingest.
    assert v.post(f"/sites/{SITE}/access/events", json=_raw()).status_code == 403
    sup = make_client(db_session, SUPERVISOR)
    # A biometric field is rejected at the API boundary (extra field forbidden).
    assert sup.post(f"/sites/{SITE}/access/events", json=_raw(face_template="x")).status_code == 422
    # Non-admin cannot change operator status.
    assert (
        sup.post(
            f"/sites/{SITE}/operators/OP-001/access-status", json={"suspended": True}
        ).status_code
        == 403
    )
