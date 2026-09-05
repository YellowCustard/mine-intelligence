"""Repositories: the only place SQL is issued for a given aggregate."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from minemonitor.contracts import AssetPositionV1
from minemonitor.storage.models import Position

# Idempotency key: replaying a position with the same (site, asset, ts) is a no-op.
_CONFLICT_KEYS = ["site_id", "asset_id", "ts"]


def insert_position(session: Session, pos: AssetPositionV1) -> bool:
    """Idempotently insert a position.

    Returns ``True`` if a new row was written, ``False`` if it already existed
    (same ``site_id``/``asset_id``/``ts``). Replaying a position never
    duplicates it (brief §12). Dialect-aware so the same path runs on Postgres
    (production) and SQLite (tests).
    """
    values = {
        "site_id": pos.site_id,
        "asset_id": pos.asset_id,
        "ts": pos.ts,
        "received_at": pos.received_at,
        "lat": pos.lat,
        "lon": pos.lon,
        "altitude_m": pos.altitude_m,
        "speed_kph": pos.speed_kph,
        "heading_deg": pos.heading_deg,
        "hdop": pos.hdop,
        "satellites": pos.satellites,
        "ignition": pos.ignition,
        "source": pos.source,
    }
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    # RETURNING tells us whether a row was actually inserted: an insert yields the
    # row, a conflict yields nothing. rowcount is unreliable here — psycopg3
    # reports -1 for INSERT ... ON CONFLICT DO NOTHING.
    stmt = (
        insert(Position)
        .values(**values)
        .on_conflict_do_nothing(index_elements=_CONFLICT_KEYS)
        .returning(Position.ts)
    )
    inserted = session.execute(stmt).first() is not None
    session.commit()
    return inserted


def list_positions(
    session: Session,
    site_id: str,
    asset_id: str | None = None,
    limit: int = 100,
) -> list[Position]:
    """Read positions for a site, newest first. Always scoped by ``site_id``."""
    stmt = select(Position).where(Position.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(Position.asset_id == asset_id)
    stmt = stmt.order_by(Position.ts.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
