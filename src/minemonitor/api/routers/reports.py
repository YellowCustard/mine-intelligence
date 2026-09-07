"""Automated shift and daily reports — JSON, CSV, and printable HTML.

All reports are pure reads over stored data (viewer-level). PDF is not generated
server-side; the printable HTML prints to PDF from any browser.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from minemonitor.auth.deps import require_viewer
from minemonitor.operations import reports as reports_mod
from minemonitor.operations.shifts import resolve_shift, resolve_shift_by_id
from minemonitor.storage.db import get_db

router = APIRouter(tags=["reports"])


def _resolve(db: Session, site_id: str, at: datetime | None, shift_id: str | None):
    if shift_id is not None:
        return resolve_shift_by_id(db, site_id, shift_id)
    return resolve_shift(db, site_id, at or datetime.now(UTC))


@router.get("/sites/{site_id}/reports/shift", dependencies=[Depends(require_viewer)])
def shift_report(
    site_id: str,
    at: datetime | None = Query(default=None),
    shift_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Full shift report as JSON (scorecard, incidents, delays, handovers)."""
    window = _resolve(db, site_id, at, shift_id)
    if window is None:
        raise HTTPException(status_code=404, detail="no shift matches")
    return reports_mod.build_shift_report(db, site_id, window)


@router.get(
    "/sites/{site_id}/reports/shift.csv",
    dependencies=[Depends(require_viewer)],
    response_class=PlainTextResponse,
)
def shift_report_csv(
    site_id: str,
    at: datetime | None = Query(default=None),
    shift_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    window = _resolve(db, site_id, at, shift_id)
    if window is None:
        raise HTTPException(status_code=404, detail="no shift matches")
    report = reports_mod.build_shift_report(db, site_id, window)
    csv_text = reports_mod.shift_report_to_csv(report)
    filename = f"shift-report-{window.shift_id.replace(':', '_')}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/sites/{site_id}/reports/shift.html",
    dependencies=[Depends(require_viewer)],
    response_class=HTMLResponse,
)
def shift_report_html(
    site_id: str,
    at: datetime | None = Query(default=None),
    shift_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Self-contained, printable HTML report (print to PDF from the browser)."""
    window = _resolve(db, site_id, at, shift_id)
    if window is None:
        raise HTTPException(status_code=404, detail="no shift matches")
    report = reports_mod.build_shift_report(db, site_id, window)
    return HTMLResponse(reports_mod.shift_report_to_html(report))


@router.get("/sites/{site_id}/reports/daily", dependencies=[Depends(require_viewer)])
def daily_report(
    site_id: str,
    date: str = Query(description="operating (local) date, YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A day's report: one shift report per shift attributed to that operating date."""
    windows = reports_mod.windows_for_date(db, site_id, date)
    if not windows:
        raise HTTPException(status_code=404, detail="no shifts for that date")
    return reports_mod.build_daily_report(db, site_id, date, windows)
