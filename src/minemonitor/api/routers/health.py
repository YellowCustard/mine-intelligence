"""Health endpoint. Green only when the database is reachable."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from minemonitor.storage.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Report service and dependency health."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001 - report any DB failure as unhealthy
        db_status = "unavailable"
    status = "ok" if db_status == "ok" else "degraded"
    return {"status": status, "db": db_status}
