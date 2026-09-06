"""Background-worker liveness heartbeats.

A worker (the ingestor) upserts its timestamp each maintenance tick; ``/health``
and the container healthcheck read it to distinguish a stuck or dead worker from
a healthy one. Kept in the database so it survives restarts and is visible to any
process, not just the one that wrote it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from minemonitor.storage.models import ServiceHeartbeat

INGESTOR = "ingestor"


def beat(session: Session, service: str, *, now: datetime | None = None) -> None:
    """Upsert a service's heartbeat timestamp. Caller commits."""
    now = now or datetime.now(UTC)
    row = session.get(ServiceHeartbeat, service)
    if row is None:
        session.add(ServiceHeartbeat(service=service, ts=now))
    else:
        row.ts = now


def age_s(session: Session, service: str, *, now: datetime | None = None) -> float | None:
    """Seconds since a service last beat, or None if it has never beat."""
    now = now or datetime.now(UTC)
    row = session.get(ServiceHeartbeat, service)
    if row is None:
        return None
    ts = row.ts if row.ts.tzinfo is not None else row.ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def is_fresh(session: Session, service: str, *, stale_s: int, now: datetime | None = None) -> bool:
    """True if the service beat within ``stale_s`` seconds."""
    age = age_s(session, service, now=now)
    return age is not None and age <= stale_s
