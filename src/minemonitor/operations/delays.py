"""Downtime / delay classification — human annotations over lost time.

*Why* time was lost is a judgement (loader busy elsewhere, a breakdown, a blast
window), not something a GNSS tracker measures. These annotations live in their
own table and never touch ``positions``, so derived analytics stay reproducible
from raw telemetry and a reclassification never rewrites history.

The category list is data, kept here so the API and the UI share one source of
truth; ``other`` and ``unknown`` are deliberate escape hatches so a supervisor is
never forced to mislabel a delay to record it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import DelayClassification

# Known delay categories. Order is display order for the UI dropdown.
DELAY_CATEGORIES: tuple[str, ...] = (
    "loader_unavailable",
    "truck_unavailable",
    "maintenance",
    "breakdown",
    "road_congestion",
    "refuelling",
    "blasting",
    "shift_change",
    "operational_delay",
    "weather",
    "other",
    "unknown",
)
_VALID: frozenset[str] = frozenset(DELAY_CATEGORIES)


def is_valid_category(category: str) -> bool:
    return category in _VALID


def create_classification(
    session: Session,
    *,
    site_id: str,
    category: str,
    start_ts: datetime,
    end_ts: datetime,
    actor: str,
    asset_id: str | None = None,
    zone_id: str | None = None,
    note: str | None = None,
    source: str = "manual",
    now: datetime | None = None,
) -> DelayClassification:
    """Record a delay classification. Caller commits.

    Raises ``ValueError`` for an unknown category or a non-positive window.
    """
    if not is_valid_category(category):
        raise ValueError(f"unknown delay category {category!r}")
    if end_ts <= start_ts:
        raise ValueError("end_ts must be after start_ts")
    row = DelayClassification(
        id=str(ULID()),
        site_id=site_id,
        asset_id=asset_id,
        zone_id=zone_id,
        category=category,
        start_ts=start_ts,
        end_ts=end_ts,
        note=note,
        source=source,
        created_by=actor,
        created_at=now or datetime.now(UTC),
    )
    session.add(row)
    return row


def get_classification(session: Session, site_id: str, id_: str) -> DelayClassification | None:
    row = session.get(DelayClassification, id_)
    if row is None or row.site_id != site_id:
        return None
    return row


def list_classifications(
    session: Session,
    site_id: str,
    *,
    category: str | None = None,
    asset_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 200,
) -> list[DelayClassification]:
    """List delay classifications for a site (always site-scoped), newest first.

    ``start``/``end`` filter to classifications overlapping that window.
    """
    stmt = select(DelayClassification).where(DelayClassification.site_id == site_id)
    if category is not None:
        stmt = stmt.where(DelayClassification.category == category)
    if asset_id is not None:
        stmt = stmt.where(DelayClassification.asset_id == asset_id)
    if start is not None:
        stmt = stmt.where(DelayClassification.end_ts > start)
    if end is not None:
        stmt = stmt.where(DelayClassification.start_ts < end)
    stmt = stmt.order_by(DelayClassification.start_ts.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
