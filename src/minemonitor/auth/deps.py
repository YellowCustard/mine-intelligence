"""FastAPI auth dependencies: HTTP Basic, role hierarchy, per-site scoping.

HTTP Basic is used so the browser dashboard authenticates with no extra JS: a 401
on the top-level page triggers the native prompt, and the cached credentials ride
every same-origin request afterwards — including the SSE ``EventSource``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from minemonitor.auth.service import authenticate
from minemonitor.storage.db import get_db
from minemonitor.storage.models import User

_security = HTTPBasic(auto_error=False)
_RANK = {"viewer": 1, "supervisor": 2, "admin": 3}
_UNAUTH = {"WWW-Authenticate": 'Basic realm="Mine Monitor"'}


def get_current_user(
    credentials: HTTPBasicCredentials | None = Depends(_security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", _UNAUTH)
    user = authenticate(db, credentials.username, credentials.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials", _UNAUTH)
    return user


def _check_site(user: User, request: Request) -> None:
    """A site-scoped user may only touch their own site's paths."""
    if user.site_id is None:
        return  # global account
    site_id = request.path_params.get("site_id")
    if site_id is not None and site_id != user.site_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorised for this site")


def require(min_role: str):
    """Dependency factory: require at least ``min_role`` (viewer<supervisor<admin)."""
    needed = _RANK[min_role]

    def dep(request: Request, user: User = Depends(get_current_user)) -> User:
        if _RANK.get(user.role, 0) < needed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires {min_role} role or higher")
        _check_site(user, request)
        return user

    return dep


def require_device(request: Request, user: User = Depends(get_current_user)) -> User:
    """Ingest endpoints: a device account (or an admin) only."""
    if user.role not in ("device", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires a device account")
    _check_site(user, request)
    return user


# Convenience dependencies.
require_viewer = require("viewer")
require_supervisor = require("supervisor")
require_admin = require("admin")
