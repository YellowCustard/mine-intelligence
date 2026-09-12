"""Optimistic concurrency on the incident lifecycle (brief §15).

Two operators who both act on a stale copy of the same incident must not silently
overwrite one another: the second write is rejected so the first is not lost.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from minemonitor.operations import incidents
from minemonitor.storage.models import Base, Site


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    seed = f()
    seed.add(Site(site_id="kn-zw-01", name="S", timezone="Africa/Harare"))
    seed.commit()
    seed.close()
    yield f
    engine.dispose()


def _open_incident(f: sessionmaker[Session]) -> str:
    s = f()
    inc = incidents.create_incident(
        s, site_id="kn-zw-01", summary="x", type_="operational", severity="info", actor="a"
    )
    s.commit()
    iid = inc.incident_id
    s.close()
    return iid


def test_concurrent_transition_is_rejected(factory: sessionmaker[Session]) -> None:
    iid = _open_incident(factory)

    # Two operators load the same open incident in separate transactions.
    s1, s2 = factory(), factory()
    inc1 = incidents.get_incident(s1, "kn-zw-01", iid)
    inc2 = incidents.get_incident(s2, "kn-zw-01", iid)
    assert inc1 is not None and inc2 is not None
    assert inc1.version_id == inc2.version_id == 1

    # Operator 1 resolves; commit succeeds and bumps the version.
    incidents.transition_incident(s1, inc1, actor="op1", to_state="resolved", resolution="fixed")
    s1.commit()

    # Operator 2, still holding the stale copy, tries to close it: rejected.
    incidents.transition_incident(s2, inc2, actor="op2", to_state="closed")
    with pytest.raises(StaleDataError):
        s2.commit()
    s2.rollback()

    # The first operator's resolution stands; nothing was silently overwritten.
    check = factory()
    final = incidents.get_incident(check, "kn-zw-01", iid)
    assert final is not None
    assert final.state == "resolved"
    assert final.resolution == "fixed"
    assert final.version_id == 2
    for s in (s1, s2, check):
        s.close()


def test_sequential_transitions_bump_version(factory: sessionmaker[Session]) -> None:
    # A well-behaved (fresh-read) sequence keeps advancing the version cleanly.
    iid = _open_incident(factory)
    s = factory()
    inc = incidents.get_incident(s, "kn-zw-01", iid)
    assert inc is not None and inc.version_id == 1
    incidents.transition_incident(s, inc, actor="op", to_state="acknowledged")
    s.commit()
    assert inc.version_id == 2
    incidents.transition_incident(s, inc, actor="op", to_state="investigating")
    s.commit()
    assert inc.version_id == 3
    s.close()
