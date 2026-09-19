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
from minemonitor.storage.models import Event, Operator, ShiftDefinition
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
        "source_system": "dahua_gate",
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
    assert kw["source_system"] == "dahua_gate" and kw["gate_id"] == "main-gate"
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


# -- slice 3: random search, metal-detector association, missed-search escalation ------


_LATE = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)  # 1h after the default passage ts (08:00Z)


def _selected_passage(db: Session, sid: str = "m1"):
    """Ingest one authorised passage deterministically selected for search (rate 100)."""
    row, _created, _alarm = service.ingest_access_event(
        db,
        SITE,
        source_system="g",
        source_event_id=sid,
        gate_id="main-gate",
        decision="granted",
        ts=_TS,
        now=_NOW,
        operator_ref="OP-001",
        search_rate_percent=100,
    )
    db.commit()
    return row


def test_random_search_selection_is_deterministic() -> None:
    assert service._select_for_search("access-x", 100) is True
    assert service._select_for_search("access-x", 0) is False
    assert service._select_for_search("access-x", 50) == service._select_for_search("access-x", 50)


def test_full_rate_selects_and_zero_rate_does_not(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    sel = _selected_passage(db_session, "s-full")
    assert sel.search_selected is True
    row, _c, _a = service.ingest_access_event(
        db_session,
        SITE,
        source_system="g",
        source_event_id="s-zero",
        gate_id="main-gate",
        decision="granted",
        ts=_TS,
        now=_NOW,
        operator_ref="OP-001",
        search_rate_percent=0,
    )
    assert row.search_selected is False


def test_complete_search_records_and_metal_alarms(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    passage = _selected_passage(db_session, "m-metal")
    row, alarm = service.complete_search(
        db_session, SITE, passage.id, metal_detected=True, now=_NOW
    )
    db_session.commit()
    assert row is not None and row.search_completed is True and row.metal_detected is True
    assert alarm is not None and alarm.type == "metal_detected" and alarm.severity == "critical"
    # Re-completing does not re-alarm (deduped per passage).
    _row2, alarm2 = service.complete_search(db_session, SITE, passage.id, metal_detected=True)
    assert alarm2 is None


def test_complete_search_missing_passage_returns_none(db_session: Session) -> None:
    assert service.complete_search(db_session, SITE, "nope", now=_NOW) == (None, None)


def test_missed_search_escalates_once_after_grace(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    _selected_passage(db_session, "miss-1")
    evs = service.detect_missed_searches(db_session, SITE, now=_LATE, grace_s=900)
    assert len(evs) == 1 and evs[0].type == "search_missed" and evs[0].severity == "warning"
    # Deduped: a re-run raises nothing while it stays open.
    assert service.detect_missed_searches(db_session, SITE, now=_LATE, grace_s=900) == []


def test_missed_search_respects_grace_window(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    _selected_passage(db_session, "miss-2")
    soon = datetime(2026, 9, 12, 8, 5, tzinfo=UTC)  # within the 15-min grace
    assert service.detect_missed_searches(db_session, SITE, now=soon, grace_s=900) == []


def test_completed_search_never_escalates(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    passage = _selected_passage(db_session, "miss-3")
    service.complete_search(db_session, SITE, passage.id, now=_NOW)
    db_session.commit()
    assert service.detect_missed_searches(db_session, SITE, now=_LATE, grace_s=900) == []


def test_api_complete_search_and_metal_alarm(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    sup = make_client(db_session, SUPERVISOR)
    eid = sup.post(f"/sites/{SITE}/access/events", json=_raw(id="api-s1")).json()["event"]["id"]
    r = sup.post(f"/sites/{SITE}/access/events/{eid}/search", json={"metal_detected": True})
    assert r.status_code == 200
    assert r.json()["alarm_raised"] is True and r.json()["event"]["search_completed"] is True
    # Viewer cannot complete a search.
    v = make_client(db_session, VIEWER)
    assert v.post(f"/sites/{SITE}/access/events/{eid}/search", json={}).status_code == 403


def test_denied_passage_is_not_auto_selected_for_search(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    # A denied attempt did not enter → never auto-selected, even at a 100% rate.
    row, _c, _a = service.ingest_access_event(
        db_session,
        SITE,
        source_system="g",
        source_event_id="d-1",
        gate_id="main-gate",
        decision="denied",
        ts=_TS,
        now=_NOW,
        operator_ref="OP-001",
        search_rate_percent=100,
    )
    assert row.search_selected is False
    # But an explicit source-provided selection is still honoured on a denied decision.
    row2, _c2, _a2 = service.ingest_access_event(
        db_session,
        SITE,
        source_system="g",
        source_event_id="d-2",
        gate_id="main-gate",
        decision="denied",
        ts=_TS,
        now=_NOW,
        operator_ref="OP-001",
        search_selected=True,
        search_rate_percent=100,
    )
    assert row2.search_selected is True


def test_metal_alarm_dedups_when_event_already_recorded(db_session: Session) -> None:
    _operator(db_session, "OP-001")
    passage = _selected_passage(db_session, "m-conc")
    # Simulate a concurrent completion having already recorded the alarm for this passage.
    db_session.add(
        Event(
            event_id=f"metal-{passage.id}",
            site_id=SITE,
            ts=_NOW,
            type="metal_detected",
            severity="critical",
            source="x",
            summary="pre-existing",
            advisory=True,
            state="open",
        )
    )
    db_session.commit()
    # Completion succeeds (no 500) and does not raise a second alarm.
    row, alarm = service.complete_search(
        db_session, SITE, passage.id, metal_detected=True, now=_NOW
    )
    db_session.commit()
    assert alarm is None and row is not None and row.metal_detected is True
