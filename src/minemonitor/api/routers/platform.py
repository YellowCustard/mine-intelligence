"""Platform introspection API (Phase 1 foundation).

Exposes the extension substrate for operators and integrators: which versioned event
contracts the platform understands, and the in-process observability counters. New
surface only — served under the ``/api/v1`` prefix.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from minemonitor.auth.deps import require_admin, require_viewer
from minemonitor.platform import contracts
from minemonitor.platform.metrics import metrics
from minemonitor.storage.models import User

router = APIRouter(tags=["platform"])


@router.get("/platform/contracts")
def list_contracts(_: User = Depends(require_viewer)) -> dict[str, Any]:
    """The versioned event contracts the platform currently understands."""
    return {"contracts": contracts.registered()}


@router.get("/platform/metrics")
def platform_metrics(_: User = Depends(require_admin)) -> dict[str, Any]:
    """Process-local observability counters (best-effort; reset on restart)."""
    return {"metrics": metrics().snapshot()}
