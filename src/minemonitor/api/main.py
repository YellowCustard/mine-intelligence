"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from minemonitor import __version__
from minemonitor.api.routers import health, ingest
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
    return app


app = create_app()
