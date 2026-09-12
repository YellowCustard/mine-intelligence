"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from minemonitor import __version__
from minemonitor.api.routers import (
    account,
    cycles,
    delays,
    devices,
    events,
    fuel,
    handovers,
    health,
    incidents,
    ingest,
    maintenance,
    notifications,
    operations,
    platform,
    reports,
    stream,
    weighbridge,
    zones,
)
from minemonitor.auth.deps import require_viewer
from minemonitor.auth.service import create_user, user_count
from minemonitor.config import get_settings
from minemonitor.logging_config import configure_logging, request_id_var, sanitize_request_id
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


async def _request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Correlation id + safe error boundary (brief §25, §3).

    Every request gets an id — a sanitised client ``X-Request-ID`` or a fresh one —
    bound to the structured log for its whole lifetime and echoed on the response.
    An unhandled exception is logged with that id and the traceback, and the client
    gets a generic 500 carrying only the id: internal detail never leaks.
    """
    rid = sanitize_request_id(request.headers.get("x-request-id")) or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "unhandled request error",
                extra={"path": request.url.path, "method": request.method},
            )
            response = JSONResponse(
                {"detail": "internal server error", "request_id": rid}, status_code=500
            )
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        request_id_var.reset(token)


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


# Versioned API prefix for new clients (mobile, integrations). Existing unprefixed
# paths remain the compatibility surface; both are served (see create_app).
API_V1 = "/api/v1"


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
    # Registered inner-first: request context (id + error boundary) runs closest to
    # the route; security headers is outermost so it also stamps the 500 the error
    # boundary produces.
    app.middleware("http")(_request_context)
    app.middleware("http")(_security_headers)
    # Domain routers. Mounted at their historical unprefixed paths (the dashboard and
    # existing clients) AND under /api/v1 (new clients: mobile, integrations). This is
    # the additive versioning shim from the evolution plan — behaviour-preserving; the
    # legacy paths keep working unchanged. See docs/PLATFORM_EVOLUTION_ARCHITECTURE.md.
    _domain_routers = [
        health.router,
        ingest.router,
        zones.router,
        events.router,
        cycles.router,
        stream.router,
        account.router,
        operations.router,
        incidents.router,
        delays.router,
        handovers.router,
        reports.router,
        devices.router,
        notifications.router,
        fuel.router,
        weighbridge.router,
        maintenance.router,
    ]
    for r in _domain_routers:
        app.include_router(r)  # legacy unprefixed
        app.include_router(r, prefix=API_V1)  # versioned
    # New Phase-1 platform surface: versioned only.
    app.include_router(platform.router, prefix=API_V1)

    @app.get("/", include_in_schema=False, dependencies=[Depends(require_viewer)])
    def dashboard() -> FileResponse:
        """Serve the operations dashboard (authenticated; same origin as the API)."""
        return FileResponse(_DASHBOARD)

    return app


app = create_app()
