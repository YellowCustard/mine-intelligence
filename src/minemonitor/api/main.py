"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse

from minemonitor import __version__
from minemonitor.api.routers import (
    account,
    cycles,
    delays,
    events,
    handovers,
    health,
    incidents,
    ingest,
    operations,
    reports,
    stream,
    zones,
)
from minemonitor.auth.deps import require_viewer
from minemonitor.auth.service import create_user, user_count
from minemonitor.config import get_settings
from minemonitor.logging_config import configure_logging
from minemonitor.storage.db import get_session_factory

# web/mine.html lives at the repo root; resolve relative to this file.
_DASHBOARD = Path(__file__).resolve().parents[3] / "web" / "mine.html"
log = logging.getLogger("minemonitor.api")

# Defence-in-depth response headers (brief §30). The dashboard is one self-contained
# file that relies on inline <script>/<style> and same-origin fetch/EventSource, so
# the CSP permits 'unsafe-inline' for scripts/styles while still forbidding any
# external origin — which is what actually contains an injected-script or
# data-exfiltration attempt. No third-party CDN is used, so 'self' is sufficient.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",  # for browsers predating frame-ancestors
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
}


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach conservative security headers to every response (brief §30)."""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _bootstrap_admin() -> None:
    """Create the bootstrap admin if configured and no users exist yet."""
    settings = get_settings()
    if not settings.bootstrap_admin_user or not settings.bootstrap_admin_password:
        return
    session = get_session_factory()()
    try:
        if user_count(session) == 0:
            create_user(
                session,
                username=settings.bootstrap_admin_user,
                password=settings.bootstrap_admin_password,
                role="admin",
            )
            session.commit()
            log.info("bootstrap admin created", extra={"user": settings.bootstrap_admin_user})
    except Exception as exc:  # noqa: BLE001 - never block startup on this
        session.rollback()
        log.warning("bootstrap admin skipped", extra={"error": str(exc)})
    finally:
        session.close()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _bootstrap_admin()
    yield


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Mine Monitor",
        version=__version__,
        description="Telemetry, geofencing and the alarm spine for mine operations.",
        lifespan=_lifespan,
    )
    app.middleware("http")(_security_headers)
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(zones.router)
    app.include_router(events.router)
    app.include_router(cycles.router)
    app.include_router(stream.router)
    app.include_router(account.router)
    app.include_router(operations.router)
    app.include_router(incidents.router)
    app.include_router(delays.router)
    app.include_router(handovers.router)
    app.include_router(reports.router)

    @app.get("/", include_in_schema=False, dependencies=[Depends(require_viewer)])
    def dashboard() -> FileResponse:
        """Serve the operations dashboard (authenticated; same origin as the API)."""
        return FileResponse(_DASHBOARD)

    return app


app = create_app()
