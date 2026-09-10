"""Health endpoints.

``/healthz`` is a liveness probe — 200 only while the API process is up and its
database is reachable — used by the api container's healthcheck. ``/health`` is
the full-system view: it also reports MQTT reachability and the ingestor's
heartbeat, and goes 503 ("red") when any of those is down, so a stuck ingestor or
a dead broker is visible even though the API itself can still serve reads.
"""

from __future__ import annotations

import socket
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from minemonitor import __version__, heartbeat
from minemonitor.config import get_settings
from minemonitor.storage.db import get_db

router = APIRouter(tags=["health"])


def _db_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any DB error means unhealthy
        return False


def _db_revision(db: Session) -> str | None:
    """The applied Alembic migration revision, or None if unavailable.

    Reported so a support engineer can tell, from one call, which schema a mine
    is actually running — different deployments drift and this pins it down.
    """
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - table absent (tests) or DB down
        return None


@router.get("/version")
def version(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Application identity and schema revision — a safe, unauthenticated probe.

    Carries no operational or personal data (brief §38): just the app name, the
    release version, and the applied database migration so multiple mine
    deployments can be told apart during support.
    """
    return {
        "name": "Mine Monitor",
        "version": __version__,
        "db_revision": _db_revision(db),
    }


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
