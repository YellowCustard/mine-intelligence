"""Repositories: the only place SQL is issued for a given aggregate."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from minemonitor.contracts import AssetPositionV1
from minemonitor.storage.models import Position, RecomputeCursor, Site

# Idempotency key: replaying a position with the same (site, asset, ts) is a no-op.
_CONFLICT_KEYS = ["site_id", "asset_id", "ts"]


def insert_position(session: Session, pos: AssetPositionV1, *, commit: bool = True) -> bool:
    """Idempotently insert a position.

    Returns ``True`` if a new row was written, ``False`` if it already existed
    (same ``site_id``/``asset_id``/``ts``). Replaying a position never
    duplicates it (brief §12). Dialect-aware so the same path runs on Postgres
    (production) and SQLite (tests).

    ``commit=False`` leaves the transaction open so the caller can insert the
    position and process its rule events atomically in one commit.
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
    if inserted:
        _mark_recompute_dirty(session, pos)
    if commit:
        session.commit()
    return inserted


def _mark_recompute_dirty(session: Session, pos: AssetPositionV1) -> None:
    """Tell the incremental recompute about a fix it would otherwise never look at.

    A fix newer than the cursor's ``through_ts`` falls inside the next window anyway, so
    the common in-order case writes nothing. A fix behind it (late, backfilled, out of
    order) lowers ``dirty_from`` so the next run widens its window back to it. A first
    fix for an asset creates the cursor. Same transaction as the position: a crash
    cannot store the fix and lose the mark.

    The upsert locks the cursor row even when it changes nothing, so it waits for a
    rebuild that is moving the cursor (the tick, or the newest slice of an on-demand
    recompute) and then compares against the ``through_ts`` it wrote: a fix can never
    slip in between a run's read of positions and its cursor update.
    """
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(RecomputeCursor).values(
        site_id=pos.site_id,
        asset_id=pos.asset_id,
        dirty_from=pos.ts.astimezone(UTC),
        entry_known=False,
    )
    cur = RecomputeCursor.__table__.c
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["site_id", "asset_id"],
            set_={"dirty_from": stmt.excluded.dirty_from},
            where=(
                or_(cur.through_ts.is_(None), stmt.excluded.dirty_from < cur.through_ts)
                & or_(cur.dirty_from.is_(None), stmt.excluded.dirty_from < cur.dirty_from)
            ),
        )
    )


def served_site_ids(session: Session, default_site_id: str) -> list[str]:
    """Every site background jobs serve: the ``sites`` table plus the configured default
    (which may have positions before anyone creates its ``sites`` row)."""
    return sorted(set(session.execute(select(Site.site_id)).scalars()) | {default_site_id})


def list_positions(
    session: Session,
    site_id: str,
    asset_id: str | None = None,
    limit: int = 100,
    since: datetime | None = None,
    until: datetime | None = None,
    order: str = "desc",
) -> list[Position]:
    """Read positions for a site. Always scoped by ``site_id``.

    ``since``/``until`` bound the device-time window (``since`` inclusive, ``until``
    exclusive) for historical playback; ``order`` is ``"desc"`` (newest first, the
    live default) or ``"asc"`` (oldest first, natural for replaying a track).
    """
    stmt = select(Position).where(Position.site_id == site_id)
    if asset_id is not None:
        stmt = stmt.where(Position.asset_id == asset_id)
    if since is not None:
        stmt = stmt.where(Position.ts >= since)
    if until is not None:
        stmt = stmt.where(Position.ts < until)
    stmt = stmt.order_by(Position.ts.asc() if order == "asc" else Position.ts.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


# The newest fix per asset without reading the whole history. A GROUP BY max(ts) visits
# every stored position for the site, so its cost grows with retention (a month of 1 Hz
# fixes is millions of rows) on every dashboard frame. This walks the primary key
# (site_id, asset_id, ts) instead: one index probe to find each next asset_id (a loose
# index scan), then one backward probe for that asset's newest fix.
_LATEST_POSITIONS_PG = text(
    """
    WITH RECURSIVE ids AS (
        (SELECT asset_id FROM positions WHERE site_id = :site_id ORDER BY asset_id LIMIT 1)
        UNION ALL
        SELECT (
            SELECT p.asset_id FROM positions p
            WHERE p.site_id = :site_id AND p.asset_id > ids.asset_id
            ORDER BY p.asset_id LIMIT 1
        )
        FROM ids WHERE ids.asset_id IS NOT NULL
    )
    SELECT latest.* FROM ids
    CROSS JOIN LATERAL (
        SELECT * FROM positions p
        WHERE p.site_id = :site_id AND p.asset_id = ids.asset_id
        ORDER BY p.ts DESC LIMIT 1
    ) AS latest
    """
)


def latest_positions(session: Session, site_id: str) -> list[Position]:
    """The most recent position for each asset at a site (for live markers)."""
    from sqlalchemy import and_, func

    if session.get_bind().dialect.name == "postgresql":
        stmt_pg = select(Position).from_statement(_LATEST_POSITIONS_PG.bindparams(site_id=site_id))
        return list(session.execute(stmt_pg).scalars().all())
    latest = (
        select(Position.asset_id, func.max(Position.ts).label("mts"))
        .where(Position.site_id == site_id)
        .group_by(Position.asset_id)
        .subquery()
    )
    stmt = select(Position).join(
        latest,
        and_(
            Position.site_id == site_id,
            Position.asset_id == latest.c.asset_id,
            Position.ts == latest.c.mts,
        ),
    )
    return list(session.execute(stmt).scalars().all())
