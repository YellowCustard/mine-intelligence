"""Personal-data breach register and compliance pack (CDPA, SI 155 of 2024).

Admin-only and path-scoped like the operators endpoints, so a site-scoped admin reaches
only their own site's breaches. The export of the people affected is a read of personal
data and is audited as one; so is each compliance pack.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import audit, compliance_pack
from minemonitor.auth.deps import require_admin
from minemonitor.breaches import service
from minemonitor.breaches.service import BreachConflict, BreachError, DataClass, Notice
from minemonitor.config import get_settings
from minemonitor.storage.db import get_db
from minemonitor.storage.models import BreachOperator, BreachRecord, Site, User

router = APIRouter(tags=["breaches"])


class BreachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovered_at: AwareDatetime
    occurred_at: AwareDatetime | None = None
    summary: str = Field(min_length=1, max_length=4000)
    data_classes: list[DataClass] = Field(min_length=1)
    operator_ids: list[str] = Field(default_factory=list, max_length=5000)
    notify_individuals: bool = True


class NoticeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notified_at: AwareDatetime
    reference: str | None = Field(default=None, max_length=200)


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=4000)


def _site(db: Session, site_id: str) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    return site


def _get(db: Session, site_id: str, breach_id: str) -> BreachRecord:
    row = db.get(BreachRecord, breach_id)
    if row is None or row.site_id != site_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "breach not found")
    return row


def _as_dict(db: Session, row: BreachRecord, *, now: datetime) -> dict[str, Any]:
    warn = timedelta(hours=get_settings().breach_warn_before_h)
    operator_ids = list(
        db.execute(
            select(BreachOperator.operator_id)
            .where(BreachOperator.breach_id == row.breach_id)
            .order_by(BreachOperator.operator_id)
        ).scalars()
    )

    def _ts(dt: datetime | None) -> datetime | None:
        return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt

    return {
        "breach_id": row.breach_id,
        "site_id": row.site_id,
        "discovered_at": _ts(row.discovered_at),
        "occurred_at": _ts(row.occurred_at),
        "summary": row.summary,
        "data_classes": row.data_classes,
        "operator_ids": operator_ids,
        "regulator": {
            "due_at": _ts(row.regulator_due_at),
            "notified_at": _ts(row.regulator_notified_at),
            "reference": row.regulator_reference,
            "status": service.notice_status(row, "regulator", now, warn),
        },
        "individuals": {
            "due_at": _ts(row.individuals_due_at),
            "notified_at": _ts(row.individuals_notified_at),
            "status": service.notice_status(row, "individuals", now, warn),
        },
        "recorded_by": row.recorded_by,
        "recorded_at": _ts(row.recorded_at),
        "closed_at": _ts(row.closed_at),
        "closed_by": row.closed_by,
        "close_note": row.close_note,
    }


def _refuse(exc: BreachError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, BreachConflict)
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, str(exc))


@router.post("/sites/{site_id}/breaches", status_code=201)
def record_breach(
    site_id: str,
    body: BreachIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Record a personal-data breach. Its notice deadlines are fixed now, from the settings."""
    _site(db, site_id)
    try:
        row = service.record_breach(
            db,
            site_id=site_id,
            discovered_at=body.discovered_at,
            occurred_at=body.occurred_at,
            summary=body.summary,
            data_classes=list(body.data_classes),
            operator_ids=body.operator_ids,
            notify_individuals=body.notify_individuals,
            actor=admin.username,
        )
    except BreachError as exc:
        db.rollback()
        raise _refuse(exc) from exc
    db.commit()
    return _as_dict(db, row, now=datetime.now(UTC))


@router.get("/sites/{site_id}/breaches", dependencies=[Depends(require_admin)])
def list_breaches(
    site_id: str,
    state: Literal["open", "closed", "all"] = Query(default="open"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The site's breach register, newest discovery first."""
    stmt = select(BreachRecord).where(BreachRecord.site_id == site_id)
    if state == "open":
        stmt = stmt.where(BreachRecord.closed_at.is_(None))
    elif state == "closed":
        stmt = stmt.where(BreachRecord.closed_at.is_not(None))
    rows = db.execute(stmt.order_by(BreachRecord.discovered_at.desc())).scalars().all()
    now = datetime.now(UTC)
    return [_as_dict(db, r, now=now) for r in rows]


@router.get("/sites/{site_id}/breaches/{breach_id}", dependencies=[Depends(require_admin)])
def get_breach(site_id: str, breach_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _as_dict(db, _get(db, site_id, breach_id), now=datetime.now(UTC))


@router.post("/sites/{site_id}/breaches/{breach_id}/notices/{notice}")
def record_notice(
    site_id: str,
    breach_id: str,
    notice: Notice,
    body: NoticeIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Record that the regulator, or the people affected, were told. Stops that notice's alarms."""
    row = _get(db, site_id, breach_id)
    try:
        service.record_notice(
            db,
            row,
            notice,
            notified_at=body.notified_at,
            reference=body.reference if notice == "regulator" else None,
            actor=admin.username,
        )
    except BreachError as exc:
        db.rollback()
        raise _refuse(exc) from exc
    db.commit()
    return _as_dict(db, row, now=datetime.now(UTC))


@router.post("/sites/{site_id}/breaches/{breach_id}/close")
def close_breach(
    site_id: str,
    breach_id: str,
    body: CloseIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Close a breach (handled, or found not to be one). Its alarms stop."""
    row = _get(db, site_id, breach_id)
    try:
        service.close_breach(db, row, note=body.note, actor=admin.username)
    except BreachError as exc:
        db.rollback()
        raise _refuse(exc) from exc
    db.commit()
    return _as_dict(db, row, now=datetime.now(UTC))


@router.get("/sites/{site_id}/breaches/{breach_id}/affected.csv", response_class=PlainTextResponse)
def affected_csv(
    site_id: str,
    breach_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> PlainTextResponse:
    """The people a breach affected, with their contact details, for the notice to them."""
    row = _get(db, site_id, breach_id)
    operators = service.affected_operators(db, row)
    audit.record(
        db,
        actor=admin.username,
        action="personal_data.export",
        entity_type="breach",
        entity_id=breach_id,
        site_id=site_id,
        detail={"operators": len(operators)},
    )
    db.commit()
    return PlainTextResponse(
        service.affected_operators_csv(operators),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="breach-{breach_id}-affected.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/sites/{site_id}/compliance-pack.zip")
def get_compliance_pack(
    site_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """The site's compliance pack for an inspector: processing, retention, audit, breaches."""
    _site(db, site_id)
    now = datetime.now(UTC)
    data, counts = compliance_pack.build(db, site_id, actor=admin.username, now=now)
    audit.record(
        db,
        actor=admin.username,
        action="compliance.pack_export",
        entity_type="site",
        entity_id=site_id,
        site_id=site_id,
        detail=counts,
    )
    db.commit()
    filename = f"compliance-pack-{site_id}-{now:%Y%m%dT%H%MZ}.zip"
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
