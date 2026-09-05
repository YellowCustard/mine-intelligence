"""The single normalise-and-store path shared by every ingest transport.

HTTP and MQTT both arrive here: validate the device payload against the contract,
stamp ``received_at`` server-side, and idempotently store. Keeping one function
means the two transports cannot drift in how they validate or what they store.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from minemonitor.contracts import AssetPositionV1
from minemonitor.storage.repositories import insert_position


class PositionIngest(BaseModel):
    """Device-facing position payload. ``received_at`` is added server-side.

    ``received_at`` is deliberately absent: it is server time, never set by the
    device (brief §6). ``extra='forbid'`` rejects unexpected device fields rather
    than silently coercing them (brief §12).
    """

    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(default="asset.position.v1", alias="schema")
    site_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
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
