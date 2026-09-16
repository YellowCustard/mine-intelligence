"""Weighbridge domain API (Phase 4) — measured tickets, tonnage, CSV import.

Reads are viewer-level; recording and import are supervisor-level and audited; scale
reference data is admin-level. Every write is site-scoped. Recording a *new* ticket
publishes ``weighbridge.transaction.v1`` on the in-process event bus (Phase 1 seam);
an idempotent re-record (same ticket_no) neither re-publishes nor double-counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minemonitor import audit
from minemonitor.auth.deps import require_admin, require_supervisor, require_viewer
from minemonitor.contracts.weighbridge import WeighbridgeTransactionV1
from minemonitor.platform.bus import bus
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User, WeighTicket
from minemonitor.weighbridge import service

router = APIRouter(tags=["weighbridge"])


class ScaleIn(BaseModel):
    scale_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class TicketIn(BaseModel):
    ticket_no: str = Field(min_length=1)
    gross_kg: float = Field(ge=0)
    tare_kg: float = Field(ge=0)
    net_kg: float
    ts: datetime | None = None
    direction: str = "outbound"
    scale_id: str | None = None
    asset_id: str | None = None
    trailer: str | None = None
    material: str | None = None
    destination: str | None = None
    customer: str | None = None
    operator_ref: str | None = None
    note: str | None = None


class ImportIn(BaseModel):
    csv: str = Field(min_length=1)


def _ticket_dict(t: WeighTicket) -> dict[str, Any]:
    return {
        "id": t.id,
        "site_id": t.site_id,
        "ticket_no": t.ticket_no,
        "scale_id": t.scale_id,
        "ts": t.ts,
        "direction": t.direction,
        "gross_kg": t.gross_kg,
        "tare_kg": t.tare_kg,
        "net_kg": t.net_kg,
        "asset_id": t.asset_id,
        "material": t.material,
        "destination": t.destination,
        "customer": t.customer,
        "net_consistency": service.net_consistency(t),
    }


@router.post("/sites/{site_id}/weighbridge/scales", status_code=201)
def create_scale(
    site_id: str,
    body: ScaleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Create/update a weighbridge scale (admin — reference data)."""
    scale = service.create_scale(
        db, scale_id=body.scale_id, site_id=site_id, name=body.name, now=datetime.now(UTC)
    )
    audit.record(
        db,
        actor=admin.username,
        action="weighbridge.scale.upsert",
        entity_type="weighbridge",
        entity_id=body.scale_id,
        site_id=site_id,
        detail={"name": body.name},
    )
    db.commit()
    return {"scale_id": scale.scale_id, "site_id": scale.site_id, "name": scale.name}


@router.get("/sites/{site_id}/weighbridge/scales")
def list_scales(
    site_id: str, db: Session = Depends(get_db), _: User = Depends(require_viewer)
) -> list[dict[str, Any]]:
    return [
        {"scale_id": s.scale_id, "site_id": s.site_id, "name": s.name}
        for s in service.list_scales(db, site_id)
    ]


def _publish(row: WeighTicket) -> None:
    bus().publish(
        WeighbridgeTransactionV1.model_validate(
            {
                "ticket_id": row.id,
                "site_id": row.site_id,
                "ticket_no": row.ticket_no,
                "ts": row.ts,
                "direction": row.direction,
                "gross_kg": row.gross_kg,
                "tare_kg": row.tare_kg,
                "net_kg": row.net_kg,
                "scale_id": row.scale_id,
                "asset_id": row.asset_id,
                "trailer": row.trailer,
                "material": row.material,
                "destination": row.destination,
                "customer": row.customer,
                "source": row.source,
            }
        )
    )


@router.post("/sites/{site_id}/weighbridge/tickets", status_code=201)
def record_ticket(
    site_id: str,
    body: TicketIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Record a measured weigh ticket (supervisor; audited; idempotent per ticket_no)."""
    now = datetime.now(UTC)
    try:
        row, created = service.record_ticket(
            db,
            site_id=site_id,
            ticket_no=body.ticket_no,
            ts=body.ts or now,
            gross_kg=body.gross_kg,
            tare_kg=body.tare_kg,
            net_kg=body.net_kg,
            created_by=user.username,
            now=now,
            direction=body.direction,
            scale_id=body.scale_id,
            asset_id=body.asset_id,
            trailer=body.trailer,
            material=body.material,
            destination=body.destination,
            customer=body.customer,
            operator_ref=body.operator_ref,
            source="api",
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if created:
        audit.record(
            db,
            actor=user.username,
            action="weighbridge.ticket.record",
            entity_type="weigh_ticket",
            entity_id=row.id,
            site_id=site_id,
            detail={"ticket_no": row.ticket_no, "net_kg": row.net_kg},
        )
    db.commit()
    if created:
        _publish(row)
    return {**_ticket_dict(row), "created": created}


@router.get("/sites/{site_id}/weighbridge/tickets")
def list_tickets(
    site_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    material: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> list[dict[str, Any]]:
    return [
        _ticket_dict(t)
        for t in service.list_tickets(
            db, site_id, since=since, until=until, material=material, limit=limit
        )
    ]


@router.post("/sites/{site_id}/weighbridge/import")
def import_csv(
    site_id: str,
    body: ImportIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict[str, Any]:
    """Import weigh tickets from CSV (supervisor; audited; idempotent per ticket_no)."""
    try:
        result = service.import_csv(
            db, site_id=site_id, csv_text=body.csv, created_by=user.username, now=datetime.now(UTC)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(
        db,
        actor=user.username,
        action="weighbridge.import",
        entity_type="site",
        entity_id=site_id,
        site_id=site_id,
        detail={"imported": result["imported"], "skipped": result["skipped"]},
    )
    db.commit()
    return result


@router.get("/sites/{site_id}/weighbridge/tonnage")
def tonnage(
    site_id: str,
    since: datetime,
    until: datetime,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer),
) -> dict[str, Any]:
    """Measured net tonnage per material over a window (with a net-consistency flag count)."""
    return service.tonnage_summary(db, site_id, since=since, until=until)
