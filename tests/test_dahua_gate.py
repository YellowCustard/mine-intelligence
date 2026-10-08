"""Live Dahua gate poller (FP-07): vendor mapping, cursor, idempotent + resilient polling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from minemonitor.ingest.adapters.dahua_gate import (
    DahuaHttpGateSource,
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
        "source_system": "dahua_gate",
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
    src = DahuaHttpGateSource("http://x", "tok")
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
    assert raw["operator_ref"] == "OP-7" and raw["source_system"] == "dahua_gate"
    assert not any("face" in k.lower() or "template" in k.lower() for k in raw)


def test_to_raw_denies_non_pass_status() -> None:
    src = DahuaHttpGateSource("http://x", "tok")
    assert src._to_raw({"RecordID": "r", "Status": "Deny"})["decision"] == "denied"


# -- HTTP fetch with an injected transport (no wire) -----------------------------------


def test_http_fetch_parses_and_passes_since() -> None:
    seen: dict[str, str] = {}

    def transport(url: str, token: str, timeout_s: float) -> bytes:
        seen["url"] = url
        return json.dumps(
            {"events": [{"RecordID": "r1", "DeviceName": "g", "AlarmTime": "t", "Status": "Pass"}]}
        ).encode()

    src = DahuaHttpGateSource("http://gate/api", "tok", transport=transport)
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

    assert _since_cursor(db_session, SITE, "dahua_gate", 30) is None
    _operator(db_session)
    poll_once(db_session, SITE, _FakeSource([_graw()]), source_system="dahua_gate", now=_NOW)
    stored = db_session.execute(select(func.max(AccessEvent.ts))).scalar_one()
    stored = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    cur = _since_cursor(db_session, SITE, "dahua_gate", 30)
    assert cur is not None and cur == stored - timedelta(seconds=30)  # last ts − overlap


def test_poll_ingests_and_is_idempotent(db_session: Session) -> None:
    _operator(db_session)
    src = _FakeSource([_graw()])
    new, alarms = poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    assert len(new) == 1 and alarms == []
    # Same event again (overlap refetch) → deduped, no new rows.
    new2, _ = poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    assert new2 == []


def test_poll_raises_alarm_for_unauthorised_grant(db_session: Session) -> None:
    _operator(db_session, "OP-001", suspended=True)  # gate grants a suspended operator
    _new, alarms = poll_once(
        db_session, SITE, _FakeSource([_graw()]), source_system="dahua_gate", now=_NOW
    )
    assert len(alarms) == 1 and alarms[0].type == "access_denied"


def test_poll_fetch_failure_is_swallowed(db_session: Session) -> None:
    new, alarms = poll_once(
        db_session, SITE, _FakeSource([], fail=True), source_system="dahua_gate", now=_NOW
    )
    assert new == [] and alarms == []  # no raise; retries next tick


def test_poll_skips_bad_event_keeps_good(db_session: Session) -> None:
    _operator(db_session)
    src = _FakeSource([_graw(id="ok"), _graw(id="bad", face_template="x")])  # one biometric payload
    new, _ = poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    assert len(new) == 1 and new[0].id.endswith("-ok")


# -- a gate clock running ahead must not blind the poller (plan G1) --------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _clock_alarms(db: Session) -> list[Any]:
    from sqlalchemy import select

    from minemonitor.storage.models import Event

    return list(db.execute(select(Event).where(Event.type == "source_clock_skew")).scalars().all())


def test_future_dated_event_does_not_blind_later_polls(db_session: Session) -> None:
    """Reproduces G1: one event a year ahead, then two genuine ones, used to give zero new."""
    from datetime import timedelta

    _operator(db_session)
    vendor = [_graw(id="future", time=_iso(_NOW + timedelta(days=365)))]
    src = SimulatedGateSource(vendor)  # honours `since`, like the vendor API
    new, _ = poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    assert new == []  # the bad-clock event is not recorded as a passage

    vendor += [
        _graw(id="real-1", time=_iso(_NOW + timedelta(minutes=1))),
        _graw(id="real-2", time=_iso(_NOW + timedelta(minutes=2))),
    ]
    later = _NOW + timedelta(minutes=3)
    new, _ = poll_once(db_session, SITE, src, source_system="dahua_gate", now=later)
    assert sorted(r.id.removeprefix(f"access-{SITE}-dahua_gate-") for r in new) == [
        "real-1",
        "real-2",
    ]


def test_future_dated_event_is_quarantined_with_one_alarm(db_session: Session) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from minemonitor.storage.models import AccessEvent, AccessQuarantine

    _operator(db_session)
    future = _NOW + timedelta(days=365)
    src = SimulatedGateSource([_graw(id="future", time=_iso(future))])
    for i in range(3):  # the vendor re-presents it on every poll
        poll_once(
            db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(minutes=i)
        )
    assert db_session.execute(select(AccessEvent)).scalars().all() == []
    q = db_session.execute(select(AccessQuarantine)).scalars().all()
    assert len(q) == 1 and q[0].gate_id == "main-gate" and q[0].source_event_id == "future"
    alarms = _clock_alarms(db_session)
    assert len(alarms) == 1
    a = alarms[0]
    assert a.severity == "warning" and a.source == "access_gate:main-gate" and a.advisory
    assert a.detail["source_event_id"] == "future" and a.detail["skew_s"] > 364 * 86400
    # No personal data rides on the alarm or the quarantine row.
    assert "operator_ref" not in a.detail and "credential_ref" not in a.detail
    assert not hasattr(q[0], "operator_ref") and not hasattr(q[0], "credential_ref")


def test_clock_alarm_is_one_per_gate_and_never_re_raised_for_a_stale_event(
    db_session: Session,
) -> None:
    from datetime import timedelta

    from minemonitor.events.repository import acknowledge_event

    _operator(db_session)
    vendor = [_graw(id="f1", time=_iso(_NOW + timedelta(days=365)))]
    src = SimulatedGateSource(vendor)
    poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    # A second bad-clock passage at the same gate while the alarm is open: quarantined, no storm.
    vendor.append(_graw(id="f2", time=_iso(_NOW + timedelta(days=365, minutes=1))))
    poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(minutes=1))
    assert len(_clock_alarms(db_session)) == 1

    # The supervisor acknowledges it; the appliance keeps re-presenting f1/f2 (still "newer"
    # than the cursor) — already quarantined, so no fresh alarm.
    acknowledge_event(db_session, SITE, _clock_alarms(db_session)[0].event_id, "sup")
    poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(minutes=2))
    assert len(_clock_alarms(db_session)) == 1

    # A new bad-clock passage after the ack means the clock is still wrong: alarm again.
    vendor.append(_graw(id="f3", time=_iso(_NOW + timedelta(days=365, minutes=2))))
    poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(minutes=3))
    assert len(_clock_alarms(db_session)) == 2

    # Another gate with a bad clock gets its own alarm.
    vendor.append(_graw(id="g1", gate_id="gold-room", time=_iso(_NOW + timedelta(days=2))))
    poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(minutes=4))
    assert {a.source for a in _clock_alarms(db_session)} == {
        "access_gate:main-gate",
        "access_gate:gold-room",
    }


def test_small_clock_lead_within_skew_does_not_hide_other_gates(db_session: Session) -> None:
    """A gate 100 s fast (inside the 120 s allowance) is recorded, but must not drag the cursor
    past events other gates record afterwards."""
    from datetime import timedelta

    _operator(db_session)
    vendor = [_graw(id="fast", gate_id="g-fast", time=_iso(_NOW + timedelta(seconds=100)))]
    src = SimulatedGateSource(vendor)
    new, _ = poll_once(db_session, SITE, src, source_system="dahua_gate", now=_NOW)
    assert len(new) == 1
    vendor.append(_graw(id="ok", gate_id="g-ok", time=_iso(_NOW + timedelta(seconds=20))))
    new, _ = poll_once(
        db_session, SITE, src, source_system="dahua_gate", now=_NOW + timedelta(seconds=60)
    )
    assert [r.gate_id for r in new] == ["g-ok"]


def test_cursor_never_runs_ahead_of_server_time(db_session: Session) -> None:
    """A future-dated row stored before this fix must not hold the cursor in the future."""
    from datetime import timedelta

    from minemonitor.storage.models import AccessEvent

    db_session.add(
        AccessEvent(
            id="access-legacy",
            site_id=SITE,
            ts=_NOW + timedelta(days=365),
            source_system="dahua_gate",
            gate_id="main-gate",
            decision="granted",
            created_at=_NOW,
        )
    )
    db_session.commit()
    cur = _since_cursor(db_session, SITE, "dahua_gate", 30)
    assert cur is not None and cur <= _NOW - timedelta(seconds=30)


# -- a gate feed outage raises an alarm (plan G2) --------------------------------------


def _feed_alarms(db: Session) -> list[Any]:
    from sqlalchemy import select

    from minemonitor.storage.models import Event

    return list(
        db.execute(select(Event).where(Event.type == "source_offline").order_by(Event.ts))
        .scalars()
        .all()
    )


def _poll_at(db: Session, minutes: float, *, fail: bool) -> None:
    from datetime import timedelta

    poll_once(
        db,
        SITE,
        _FakeSource([], fail=fail),
        source_system="dahua_gate",
        now=_NOW + timedelta(minutes=minutes),
        offline_after_s=600,
    )


def test_feed_outage_raises_one_alarm_after_threshold(db_session: Session) -> None:
    _poll_at(db_session, 0, fail=False)  # the feed was up
    for m in (1, 5, 10):  # failing, but not yet for 10 minutes
        _poll_at(db_session, m, fail=True)
    assert _feed_alarms(db_session) == []
    _poll_at(db_session, 11, fail=True)  # 10 min since the first failed poll
    _poll_at(db_session, 12, fail=True)
    alarms = _feed_alarms(db_session)
    assert len(alarms) == 1
    a = alarms[0]
    assert a.severity == "warning" and a.advisory and a.state == "open"
    assert a.source == "access_feed:dahua_gate" and a.asset_id is None
    assert a.detail["failing_since"].startswith("2026-09-12T09:01")
    assert "gate link down" in a.detail["last_error"]


def test_feed_never_reached_since_start_still_alarms(db_session: Session) -> None:
    for m in (0, 6, 10):
        _poll_at(db_session, m, fail=True)
    assert len(_feed_alarms(db_session)) == 1


def test_feed_recovery_rearms_and_marks_the_alarm(db_session: Session) -> None:
    from minemonitor.events.repository import acknowledge_event

    for m in (0, 10):
        _poll_at(db_session, m, fail=True)
    first = _feed_alarms(db_session)[0]
    acknowledge_event(db_session, SITE, first.event_id, "sup")
    _poll_at(db_session, 15, fail=False)  # feed restored
    db_session.refresh(first)
    assert first.detail["recovered_at"].startswith("2026-09-12T09:15")
    assert "restored" in first.summary
    # A second outage is a new alarm, not swallowed by the first (acknowledged) one.
    for m in (20, 31):
        _poll_at(db_session, m, fail=True)
    assert len(_feed_alarms(db_session)) == 2


def test_short_blips_never_alarm(db_session: Session) -> None:
    for m in range(0, 60, 3):  # fails every other poll, never 10 minutes in a row
        _poll_at(db_session, m, fail=(m // 3) % 2 == 0)
    assert _feed_alarms(db_session) == []


def test_feed_outage_is_per_site(db_session: Session) -> None:
    from datetime import timedelta

    from minemonitor.storage.models import Site

    db_session.add(Site(site_id="other-site", name="O", timezone="Africa/Harare"))
    db_session.commit()
    for m in (0, 10):
        _poll_at(db_session, m, fail=True)
        poll_once(
            db_session,
            "other-site",
            _FakeSource([]),
            source_system="dahua_gate",
            now=_NOW + timedelta(minutes=m),
            offline_after_s=600,
        )
    alarms = _feed_alarms(db_session)
    assert [a.site_id for a in alarms] == [SITE]
