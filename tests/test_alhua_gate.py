"""Live Alhua gate poller (FP-07): vendor mapping, cursor, idempotent + resilient polling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from minemonitor.ingest.adapters.alhua_gate import (
    AlhuaHttpGateSource,
    SimulatedGateSource,
    _since_cursor,
    poll_once,
)
from minemonitor.storage.models import Operator

SITE = "kn-zw-01"
_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def _operator(db: Session, oid: str = "OP-001", *, suspended: bool = False) -> None:
    db.add(
        Operator(
            operator_id=oid, site_id=SITE, display_name="T", suspended=suspended, created_at=_NOW
        )
    )
    db.commit()


def _graw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "r1",
        "source_system": "alhua_gate",
        "gate_id": "main-gate",
        "time": "2026-09-12T10:00:07+02:00",
        "decision": "granted",
        "operator_ref": "OP-001",
    }
    base.update(over)
    return base


class _FakeSource:
    def __init__(self, raws: list[dict[str, Any]], *, fail: bool = False) -> None:
        self._raws = raws
        self._fail = fail
        self.since_seen: Any = "unset"

    def fetch(self, since: datetime | None) -> list[dict[str, Any]]:
        self.since_seen = since
        if self._fail:
            raise RuntimeError("gate link down")
        return list(self._raws)


# -- vendor mapping (verify against the real API) --------------------------------------


def test_to_raw_maps_vendor_event_and_carries_no_biometric() -> None:
    src = AlhuaHttpGateSource("http://x", "tok")
    raw = src._to_raw(
        {
            "RecordID": "r9",
            "DeviceName": "gold-room-gate",
            "AlarmTime": "2026-09-12T10:00:07+02:00",
            "Status": "Pass",
            "CardNo": "card-9",
            "PersonRef": "OP-7",
            "FaceTemplate": "<blob>",  # a vendor extra — must not be carried through
        }
    )
    assert raw["id"] == "r9" and raw["gate_id"] == "gold-room-gate"
    assert raw["decision"] == "granted" and raw["credential_ref"] == "card-9"
    assert raw["operator_ref"] == "OP-7" and raw["source_system"] == "alhua_gate"
    assert not any("face" in k.lower() or "template" in k.lower() for k in raw)


def test_to_raw_denies_non_pass_status() -> None:
    src = AlhuaHttpGateSource("http://x", "tok")
    assert src._to_raw({"RecordID": "r", "Status": "Deny"})["decision"] == "denied"


# -- HTTP fetch with an injected transport (no wire) -----------------------------------


def test_http_fetch_parses_and_passes_since() -> None:
    seen: dict[str, str] = {}

    def transport(url: str, token: str, timeout_s: float) -> bytes:
        seen["url"] = url
        return json.dumps(
            {"events": [{"RecordID": "r1", "DeviceName": "g", "AlarmTime": "t", "Status": "Pass"}]}
        ).encode()

    src = AlhuaHttpGateSource("http://gate/api", "tok", transport=transport)
    raws = src.fetch(datetime(2026, 9, 12, 8, 0, tzinfo=UTC))
    assert len(raws) == 1 and raws[0]["id"] == "r1" and raws[0]["decision"] == "granted"
    assert "since=" in seen["url"]


def test_simulated_source_filters_by_since() -> None:
    events = [
        _graw(id="a", time="2026-09-12T06:00:00+02:00"),
        _graw(id="b", time="2026-09-12T12:00:00+02:00"),
    ]
    src = SimulatedGateSource(events)
    since = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)  # between a (04:00Z) and b (10:00Z)
    got = {e["id"] for e in src.fetch(since)}
    assert got == {"b"}
    assert {e["id"] for e in src.fetch(None)} == {"a", "b"}


# -- polling: ingest, cursor, idempotency, resilience ----------------------------------


def test_since_cursor_none_then_derived(db_session: Session) -> None:
    from datetime import timedelta

    from sqlalchemy import func, select

    from minemonitor.storage.models import AccessEvent

    assert _since_cursor(db_session, SITE, "alhua_gate", 30) is None
    _operator(db_session)
    poll_once(db_session, SITE, _FakeSource([_graw()]), source_system="alhua_gate", now=_NOW)
    stored = db_session.execute(select(func.max(AccessEvent.ts))).scalar_one()
    stored = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    cur = _since_cursor(db_session, SITE, "alhua_gate", 30)
    assert cur is not None and cur == stored - timedelta(seconds=30)  # last ts − overlap


def test_poll_ingests_and_is_idempotent(db_session: Session) -> None:
    _operator(db_session)
    src = _FakeSource([_graw()])
    new, alarms = poll_once(db_session, SITE, src, source_system="alhua_gate", now=_NOW)
    assert len(new) == 1 and alarms == []
    # Same event again (overlap refetch) → deduped, no new rows.
    new2, _ = poll_once(db_session, SITE, src, source_system="alhua_gate", now=_NOW)
    assert new2 == []


def test_poll_raises_alarm_for_unauthorised_grant(db_session: Session) -> None:
    _operator(db_session, "OP-001", suspended=True)  # gate grants a suspended operator
    _new, alarms = poll_once(
        db_session, SITE, _FakeSource([_graw()]), source_system="alhua_gate", now=_NOW
    )
    assert len(alarms) == 1 and alarms[0].type == "access_denied"


def test_poll_fetch_failure_is_swallowed(db_session: Session) -> None:
    new, alarms = poll_once(
        db_session, SITE, _FakeSource([], fail=True), source_system="alhua_gate", now=_NOW
    )
    assert new == [] and alarms == []  # no raise; retries next tick


def test_poll_skips_bad_event_keeps_good(db_session: Session) -> None:
    _operator(db_session)
    src = _FakeSource([_graw(id="ok"), _graw(id="bad", face_template="x")])  # one biometric payload
    new, _ = poll_once(db_session, SITE, src, source_system="alhua_gate", now=_NOW)
    assert len(new) == 1 and new[0].id.endswith("-ok")
