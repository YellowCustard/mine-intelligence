"""Zone-occupancy breach detection (RAN Mines Phase-1 personnel accountability).

"N people in an (N-1)-capacity sector = breach": a cross-asset aggregate over confirmed
zone membership, deduped to one open alarm per zone, advisory.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from minemonitor.rules.occupancy import detect_zone_occupancy
from minemonitor.storage.models import AssetZoneState, Zone
from minemonitor.zones.occupancy import occupancy_status
from tests.conftest import ADMIN, VIEWER, make_client

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


# -- occupancy status helper -----------------------------------------------------------


def test_occupancy_status_reports_count_and_over(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 1, "severity": "critical"})
    _occupy(db_session, "sector-a", 2)
    status = occupancy_status(db_session, SITE)
    assert len(status) == 1
    z = status[0]
    assert z["zone_id"] == "sector-a" and z["occupancy"] == 2 and z["max_occupancy"] == 1
    assert z["over"] is True and z["severity"] == "critical"


def test_occupancy_status_omits_uncapped_zones(db_session: Session) -> None:
    _zone(db_session, zone_id="z1", name="Capped", rules={"max_occupancy": 3})
    _zone(db_session, zone_id="z2", name="Uncapped", rules={})
    assert [z["zone_id"] for z in occupancy_status(db_session, SITE)] == ["z1"]


# -- config + read API (RBAC, validation, site-scoping) --------------------------------


def test_api_set_and_read_occupancy(db_session: Session) -> None:
    _zone(db_session, rules={})  # no cap yet
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/zones/sector-a/occupancy", json={"max_occupancy": 5})
    assert r.status_code == 200, r.text
    assert r.json()["rules"]["max_occupancy"] == 5
    _occupy(db_session, "sector-a", 6)
    viewer = make_client(db_session, VIEWER)
    g = viewer.get(f"/api/v1/sites/{SITE}/zones/occupancy")
    assert g.status_code == 200
    assert g.json() == [
        {
            "zone_id": "sector-a",
            "name": "Sector A",
            "max_occupancy": 5,
            "occupancy": 6,
            "over": True,
            "severity": "warning",
        }
    ]


def test_api_clear_cap_removes_optin(db_session: Session) -> None:
    _zone(db_session, rules={"max_occupancy": 5, "severity": "critical"})
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/zones/sector-a/occupancy", json={"max_occupancy": None})
    assert r.status_code == 200
    assert "max_occupancy" not in r.json()["rules"] and "severity" not in r.json()["rules"]
    assert occupancy_status(db_session, SITE) == []  # opted out


def test_api_rejects_negative_cap(db_session: Session) -> None:
    _zone(db_session, rules={})
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/zones/sector-a/occupancy", json={"max_occupancy": -1})
    assert r.status_code == 422  # request-model ge=0 guard


def test_api_unknown_zone_is_404(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    r = admin.put(f"/api/v1/sites/{SITE}/zones/nope/occupancy", json={"max_occupancy": 1})
    assert r.status_code == 404


def test_api_viewer_cannot_set_capacity(db_session: Session) -> None:
    _zone(db_session, rules={})
    viewer = make_client(db_session, VIEWER)
    r = viewer.put(f"/api/v1/sites/{SITE}/zones/sector-a/occupancy", json={"max_occupancy": 1})
    assert r.status_code in (401, 403)
