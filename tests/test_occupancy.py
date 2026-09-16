"""Zone-occupancy breach detection (RAN Mines Phase-1 personnel accountability).

"N people in an (N-1)-capacity sector = breach": a cross-asset aggregate over confirmed
zone membership, deduped to one open alarm per zone, advisory.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from minemonitor.rules.occupancy import detect_zone_occupancy
from minemonitor.storage.models import AssetZoneState, Zone

_NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
SITE = "kn-zw-01"


def _zone(db: Session, *, zone_id: str = "sector-a", name: str = "Sector A", rules: dict) -> None:
    db.add(
        Zone(zone_id=zone_id, site_id=SITE, name=name, kind="restricted", geometry={}, rules=rules)
    )
    db.commit()


def _occupy(db: Session, zone_id: str, n: int, *, inside: bool = True, prefix: str = "IN") -> None:
    for i in range(n):
        db.add(
            AssetZoneState(site_id=SITE, asset_id=f"{prefix}-{i}", zone_id=zone_id, inside=inside)
        )
    db.commit()


def test_breach_fires_once_when_over_capacity(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 5, "severity": "critical"})
    _occupy(db_session, "sector-a", 6)
    evs = detect_zone_occupancy(db_session, SITE, now=_NOW)
    assert len(evs) == 1
    e = evs[0]
    assert e.type == "zone_occupancy" and e.severity == "critical"
    assert e.zone_id == "sector-a" and e.asset_id is None and e.advisory is True
    assert e.detail == {"occupancy": 6, "max_occupancy": 5}
    # Deduped: while the alarm is open, a re-run raises nothing.
    assert detect_zone_occupancy(db_session, SITE, now=_NOW) == []


def test_no_breach_at_or_under_capacity(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 5})
    _occupy(db_session, "sector-a", 5)  # exactly at cap → not a breach
    assert detect_zone_occupancy(db_session, SITE, now=_NOW) == []


def test_only_confirmed_inside_members_count(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 2})
    _occupy(db_session, "sector-a", 2, inside=True, prefix="IN")
    _occupy(db_session, "sector-a", 3, inside=False, prefix="OUT")  # left the zone
    assert detect_zone_occupancy(db_session, SITE, now=_NOW) == []


def test_zone_without_cap_is_ignored(db_session: Session) -> None:
    _zone(db_session, rules={})  # opts out
    _occupy(db_session, "sector-a", 50)
    assert detect_zone_occupancy(db_session, SITE, now=_NOW) == []


def test_malformed_cap_is_ignored_not_guessed(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": "lots"})
    _occupy(db_session, "sector-a", 9)
    assert detect_zone_occupancy(db_session, SITE, now=_NOW) == []


def test_default_severity_is_warning(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 1})
    _occupy(db_session, "sector-a", 2)
    evs = detect_zone_occupancy(db_session, SITE, now=_NOW)
    assert evs and evs[0].severity == "warning"
