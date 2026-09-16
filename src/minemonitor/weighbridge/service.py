"""Weighbridge domain service. Callers commit.

Recording is measured-only and idempotent per (site, ticket_no). ``net_consistency``
flags a ticket whose printed net disagrees with gross − tare (a data-quality signal,
never a correction). ``tonnage_summary`` aggregates measured net per material.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor.storage.models import Weighbridge, WeighTicket

_VALID_DIRECTIONS = {"inbound", "outbound"}


def create_scale(
    session: Session, *, scale_id: str, site_id: str, name: str, now: datetime
) -> Weighbridge:
    scale = session.get(Weighbridge, scale_id)
    if scale is None:
        scale = Weighbridge(scale_id=scale_id, created_at=now)
        session.add(scale)
    scale.site_id = site_id
    scale.name = name
    return scale


def list_scales(session: Session, site_id: str) -> list[Weighbridge]:
    rows = session.execute(
        select(Weighbridge).where(Weighbridge.site_id == site_id).order_by(Weighbridge.scale_id)
    )
    return list(rows.scalars().all())


def get_ticket(session: Session, site_id: str, ticket_no: str) -> WeighTicket | None:
    return session.execute(
        select(WeighTicket).where(
            WeighTicket.site_id == site_id, WeighTicket.ticket_no == ticket_no
        )
    ).scalar_one_or_none()


def record_ticket(
    session: Session,
    *,
    site_id: str,
    ticket_no: str,
    ts: datetime,
    gross_kg: float,
    tare_kg: float,
    net_kg: float,
    created_by: str,
    now: datetime,
    direction: str = "outbound",
    scale_id: str | None = None,
    asset_id: str | None = None,
    trailer: str | None = None,
    material: str | None = None,
    destination: str | None = None,
    customer: str | None = None,
    operator_ref: str | None = None,
    source: str = "manual",
    note: str | None = None,
) -> tuple[WeighTicket, bool]:
    """Record a measured weigh ticket. Returns (ticket, created).

    Idempotent: a ticket_no already present at the site is returned untouched with
    ``created=False`` (a re-import never double-counts production). Validates
    direction and non-negative weights; net is stored as given (measured), and any
    net vs gross−tare discrepancy is surfaced by ``net_consistency`` rather than fixed.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}")
    # A net vs gross−tare *mismatch* stays flaggable (net_consistency), but a negative
    # physical net weight is invalid input and is rejected so it cannot corrupt totals.
    if gross_kg < 0 or tare_kg < 0 or net_kg < 0:
        raise ValueError("gross_kg, tare_kg and net_kg must be non-negative")
    existing = get_ticket(session, site_id, ticket_no)
    if existing is not None:
        return existing, False
    row = WeighTicket(
        id=str(ULID()),
        site_id=site_id,
        ticket_no=ticket_no,
        scale_id=scale_id,
        ts=ts,
        direction=direction,
        gross_kg=gross_kg,
        tare_kg=tare_kg,
        net_kg=net_kg,
        asset_id=asset_id,
        trailer=trailer,
        material=material,
        destination=destination,
        customer=customer,
        operator_ref=operator_ref,
        source=source,
        note=note,
        created_at=now,
        created_by=created_by,
    )
    session.add(row)
    return row, True


def net_consistency(ticket: WeighTicket, *, tolerance_kg: float = 20.0) -> dict[str, Any]:
    """Flag a ticket whose printed net disagrees with gross − tare (data quality)."""
    expected = ticket.gross_kg - ticket.tare_kg
    delta = round(ticket.net_kg - expected, 3)
    return {
        "ticket_no": ticket.ticket_no,
        "consistent": abs(delta) <= tolerance_kg,
        "delta_kg": delta,
        "expected_net_kg": round(expected, 3),
        "net_kg": ticket.net_kg,
    }


def list_tickets(
    session: Session,
    site_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    material: str | None = None,
    limit: int = 200,
) -> list[WeighTicket]:
    stmt = select(WeighTicket).where(WeighTicket.site_id == site_id)
    if since is not None:
        stmt = stmt.where(WeighTicket.ts >= since)
    if until is not None:
        stmt = stmt.where(WeighTicket.ts < until)
    if material is not None:
        stmt = stmt.where(WeighTicket.material == material)
    stmt = stmt.order_by(WeighTicket.ts.desc()).limit(min(limit, 1000))
    return list(session.execute(stmt).scalars().all())


def tonnage_summary(
    session: Session,
    site_id: str,
    *,
    since: datetime,
    until: datetime,
    tolerance_kg: float = 20.0,
) -> dict[str, Any]:
    """Measured net tonnage per material over a window, plus a net-consistency flag count.

    Aggregates the **full** filtered set in SQL, not a paginated list — a long reporting
    window with many thousands of tickets is summed completely, never silently truncated to
    the newest N (the failure a list-limit would cause for exactly the windows this serves).
    """
    window = (
        WeighTicket.site_id == site_id,
        WeighTicket.ts >= since,
        WeighTicket.ts < until,
    )
    mat_rows = session.execute(
        select(
            WeighTicket.material,
            func.count().label("tickets"),
            func.coalesce(func.sum(WeighTicket.net_kg), 0.0).label("net_kg"),
        )
        .where(*window)
        .group_by(WeighTicket.material)
    ).all()
    raw: dict[str, dict[str, float]] = {}
    total_tickets = 0
    for material, tickets, net_kg in mat_rows:
        key = material or "unspecified"
        total_tickets += int(tickets)
        agg = raw.setdefault(key, {"tickets": 0.0, "net_kg": 0.0})
        agg["tickets"] += int(tickets)
        agg["net_kg"] += float(net_kg)
    materials = {
        m: {
            "tickets": int(v["tickets"]),
            "net_kg": round(v["net_kg"], 3),
            "net_tonnes": round(v["net_kg"] / 1000, 3),
            "basis": "measured",
        }
        for m, v in sorted(raw.items())
    }
    # Consistency flag over the full window too (net vs gross − tare beyond tolerance).
    flagged = int(
        session.execute(
            select(func.count())
            .select_from(WeighTicket)
            .where(
                *window,
                func.abs(WeighTicket.net_kg - (WeighTicket.gross_kg - WeighTicket.tare_kg))
                > tolerance_kg,
            )
        ).scalar_one()
    )
    return {
        "window": {"since": since, "until": until},
        "tickets": total_tickets,
        "materials": materials,
        "net_inconsistent_tickets": flagged,
    }


# --- CSV adapter (one concrete, manufacturer-neutral adapter) -----------------

_REQUIRED_COLS = {"ticket_no", "ts", "gross_kg", "tare_kg", "net_kg"}


def import_csv(
    session: Session, *, site_id: str, csv_text: str, created_by: str, now: datetime
) -> dict[str, Any]:
    """Import weigh tickets from CSV. Idempotent per ticket_no; per-row errors collected.

    Expected headers: ticket_no, ts (ISO-8601), gross_kg, tare_kg, net_kg, and optional
    direction, scale_id, asset_id, trailer, material, destination, customer.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    header = set(reader.fieldnames or [])
    if not header >= _REQUIRED_COLS:
        missing = sorted(_REQUIRED_COLS - header)
        raise ValueError(f"CSV missing required columns: {missing}")
    imported = skipped = 0
    errors: list[dict[str, Any]] = []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        try:
            _row, created = record_ticket(
                session,
                site_id=site_id,
                ticket_no=(raw.get("ticket_no") or "").strip(),
                ts=datetime.fromisoformat((raw["ts"]).replace("Z", "+00:00")),
                gross_kg=float(raw["gross_kg"]),
                tare_kg=float(raw["tare_kg"]),
                net_kg=float(raw["net_kg"]),
                created_by=created_by,
                now=now,
                direction=(raw.get("direction") or "outbound").strip() or "outbound",
                scale_id=(raw.get("scale_id") or None),
                asset_id=(raw.get("asset_id") or None),
                trailer=(raw.get("trailer") or None),
                material=(raw.get("material") or None),
                destination=(raw.get("destination") or None),
                customer=(raw.get("customer") or None),
                source="import",
            )
        except (ValueError, KeyError) as exc:
            errors.append({"row": i, "error": str(exc)})
            continue
        if created:
            imported += 1
        else:
            skipped += 1
    return {"imported": imported, "skipped": skipped, "errors": errors}
