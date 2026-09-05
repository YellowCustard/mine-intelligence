"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from minemonitor import __version__
from minemonitor.api.routers import cycles, events, health, ingest, zones
from minemonitor.config import get_settings
from minemonitor.logging_config import configure_logging


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Mine Monitor",
        version=__version__,
        description="Telemetry, geofencing and the alarm spine for mine operations.",
    )
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(zones.router)
    app.include_router(events.router)
    app.include_router(cycles.router)
    return app


app = create_app()
