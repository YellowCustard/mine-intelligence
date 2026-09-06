"""Health endpoints.

``/healthz`` is a liveness probe — 200 only while the API process is up and its
database is reachable — used by the api container's healthcheck. ``/health`` is
the full-system view: it also reports MQTT reachability and the ingestor's
heartbeat, and goes 503 ("red") when any of those is down, so a stuck ingestor or
a dead broker is visible even though the API itself can still serve reads.
"""

from __future__ import annotations

import socket

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from minemonitor import heartbeat
from minemonitor.config import get_settings
from minemonitor.storage.db import get_db

router = APIRouter(tags=["health"])


def _db_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any DB error means unhealthy
        return False


def _mqtt_reachable(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@router.get("/healthz")
def liveness(db: Session = Depends(get_db)) -> JSONResponse:
    """Liveness: the API is up and its database is reachable."""
    ok = _db_ok(db)
    body = {"status": "ok" if ok else "unavailable", "db": "ok" if ok else "unavailable"}
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/health")
def health(db: Session = Depends(get_db)) -> JSONResponse:
    """Full-system health: database, MQTT broker, and the ingestor heartbeat."""
    settings = get_settings()
    db_ok = _db_ok(db)
    mqtt_ok = _mqtt_reachable(settings.mqtt_host, settings.mqtt_port)
    # The heartbeat lookup needs the DB; report unknown if the DB itself is down.
    ingestor_fresh = db_ok and heartbeat.is_fresh(
        db, heartbeat.INGESTOR, stale_s=settings.heartbeat_stale_s
    )

    components = {
        "db": "ok" if db_ok else "unavailable",
        "mqtt": "ok" if mqtt_ok else "unavailable",
        "ingestor": "ok" if ingestor_fresh else "stale",
    }
    healthy = db_ok and mqtt_ok and ingestor_fresh
    body = {"status": "ok" if healthy else "degraded", **components}
    return JSONResponse(body, status_code=200 if healthy else 503)
