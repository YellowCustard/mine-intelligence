"""FastAPI application entrypoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from minemonitor import __version__
from minemonitor.api.routers import cycles, events, health, ingest, stream, zones
from minemonitor.config import get_settings
from minemonitor.logging_config import configure_logging

# web/mine.html lives at the repo root; resolve relative to this file.
_DASHBOARD = Path(__file__).resolve().parents[3] / "web" / "mine.html"


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
    app.include_router(stream.router)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Serve the operations dashboard (same origin as the API)."""
        return FileResponse(_DASHBOARD)

    return app


app = create_app()
