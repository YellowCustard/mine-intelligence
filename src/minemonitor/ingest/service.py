"""The single normalise-and-store path shared by every ingest transport.

HTTP and MQTT both arrive here: validate the device payload against the contract,
stamp ``received_at`` server-side, and idempotently store. Keeping one function
means the two transports cannot drift in how they validate or what they store.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor.config import get_settings
from minemonitor.contracts import AssetPositionV1
from minemonitor.contracts.ids import Identifier
from minemonitor.storage.repositories import insert_position


class PositionIngest(BaseModel):
    """Device-facing position payload. ``received_at`` is added server-side.

    ``received_at`` is deliberately absent: it is server time, never set by the
    device (brief §6). ``extra='forbid'`` rejects unexpected device fields rather
    than silently coercing them (brief §12).
    """

    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(default="asset.position.v1", alias="schema")
    site_id: Identifier
    asset_id: Identifier
    ts: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float | None = None
    speed_kph: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    hdop: float | None = Field(default=None, ge=0)
    satellites: int | None = Field(default=None, ge=0)
    ignition: bool | None = None
    source: str | None = None


class FutureTimestampError(ValueError):
    """The device clock is ahead of the server by more than the allowed skew."""


def reject_if_future(payload: PositionIngest, *, now: datetime | None = None) -> None:
    """Refuse a fix dated beyond ``now + max_future_skew_s`` (a bad device clock).

    Called by every transport *before* storing. A future-dated fix, once stored, moves the
    asset's live-rule watermark (``asset_zone_state.last_ts``) past real time, after which
    every genuine fix is skipped as out-of-order and no zone alarm fires for that asset.
    Rejected loudly rather than clamped: the true time is unknown (brief §12).
    """
    now = now or datetime.now(UTC)
    # Naive timestamps are a separate contract question; compare them as UTC here so this
    # check can never itself raise on a naive/aware comparison.
    ts = payload.ts if payload.ts.tzinfo is not None else payload.ts.replace(tzinfo=UTC)
    skew_s = (ts - now).total_seconds()
    if skew_s > get_settings().max_future_skew_s:
        raise FutureTimestampError(
            f"device ts {payload.ts.isoformat()} is {skew_s:.0f}s ahead of server time"
        )


def to_canonical(payload: PositionIngest) -> AssetPositionV1:
    """Stamp ``received_at`` and produce the canonical, stored contract shape."""
    return AssetPositionV1(
        **payload.model_dump(by_alias=True),
        received_at=datetime.now(UTC),
    )


def store_position(session: Session, payload: PositionIngest) -> bool:
    """Validate-and-store a device position. Returns True if newly inserted."""
    return insert_position(session, to_canonical(payload))


def store_and_process(session: Session, payload: PositionIngest) -> tuple[bool, list]:
    """Store a position and run zone/rule processing in one transaction.

    Returns ``(created, events)``. The position insert and any events it raises
    commit together, so a crash cannot store a position while losing its events.
    """
    from minemonitor.pipeline import process_position

    canonical = to_canonical(payload)
    created = insert_position(session, canonical, commit=False)
    events = process_position(session, canonical, created)
    session.commit()
    return created, events
