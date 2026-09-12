"""Post-deploy smoke test (brief §42).

Runs a short sequence of checks against a *running* deployment and returns a clear
pass/fail per check with a process exit code — the thing an operator runs after
``deploy.sh`` to confirm the box is actually serving, authenticating, ingesting and
showing the dashboard.

The checks are written against a small HTTP protocol (:class:`Http`) so the exact
same sequence runs against the real server (``UrllibHttp``) in production and against
the in-process app (a test client) under pytest — the test proves the smoke test.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

Auth = tuple[str, str] | None


@dataclass(frozen=True)
class Resp:
    status: int
    body: Any
    text: str


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


class Http(Protocol):
    def get(self, path: str, *, auth: Auth = None) -> Resp: ...
    def post(self, path: str, body: dict[str, Any], *, auth: Auth = None) -> Resp: ...


class UrllibHttp:
    """Standard-library HTTP client against a live base URL (no extra deps)."""

    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _do(self, method: str, path: str, body: dict[str, Any] | None, auth: Auth) -> Resp:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth is not None:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                raw = r.read().decode()
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            status = e.code
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            parsed = None
        return Resp(status=status, body=parsed, text=raw)

    def get(self, path: str, *, auth: Auth = None) -> Resp:
        return self._do("GET", path, None, auth)

    def post(self, path: str, body: dict[str, Any], *, auth: Auth = None) -> Resp:
        return self._do("POST", path, body, auth)


def run_checks(http: Http, *, site_id: str, username: str, password: str) -> list[Check]:
    """Run the deployment checks and return one :class:`Check` per step."""
    auth = (username, password)
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name=name, ok=ok, detail=detail))

    v = http.get("/version")
    add(
        "version",
        v.status == 200 and isinstance(v.body, dict) and v.body.get("name") == "Mine Monitor",
        f"{v.status} · {v.body.get('version') if isinstance(v.body, dict) else '?'}",
    )

    h = http.get("/healthz")
    add("health", h.status == 200, f"{h.status} · {h.body}")

    unauth = http.get(f"/sites/{site_id}/state")
    add("auth-enforced", unauth.status == 401, f"unauthenticated → {unauth.status} (want 401)")

    me = http.get("/me", auth=auth)
    add(
        "login",
        me.status == 200 and isinstance(me.body, dict) and me.body.get("username") == username,
        f"{me.status} · role={me.body.get('role') if isinstance(me.body, dict) else '?'}",
    )

    # Ingest round-trip: post one position for a dedicated smoke asset, read it back.
    ts = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    pos = {
        "schema": "asset.position.v1",
        "site_id": site_id,
        "asset_id": "SMOKE-TEST",
        "ts": ts,
        "lat": -17.8252,
        "lon": 31.0335,
        "source": "smoke",
    }
    ing = http.post("/ingest/positions", pos, auth=auth)
    read = http.get(f"/sites/{site_id}/positions?asset_id=SMOKE-TEST&limit=5", auth=auth)
    found = (
        read.status == 200
        and isinstance(read.body, list)
        and any(r.get("ts", "").startswith(ts[:16]) for r in read.body)
    )
    add("ingest-roundtrip", ing.status == 202 and found, f"post={ing.status} · readback={found}")

    dash = http.get("/", auth=auth)
    add("dashboard", dash.status == 200 and "mine monitor" in dash.text.lower(), f"{dash.status}")

    return checks


def format_report(checks: list[Check]) -> str:
    lines = [f"[{'PASS' if c.ok else 'FAIL'}] {c.name:<16} {c.detail}" for c in checks]
    ok = sum(c.ok for c in checks)
    lines.append(f"\n{ok}/{len(checks)} checks passed")
    return "\n".join(lines)


def main() -> int:
    """CLI entrypoint: read config from the environment, run, print, exit 0/1."""
    base = os.environ.get("MM_SMOKE_URL", "http://127.0.0.1:8000")
    user = os.environ.get("MM_SMOKE_USER", "")
    pwd = os.environ.get("MM_SMOKE_PASSWORD", "")
    site = os.environ.get("MM_SMOKE_SITE", "kn-zw-01")
    if not user or not pwd:
        print("set MM_SMOKE_USER and MM_SMOKE_PASSWORD (and optionally MM_SMOKE_URL/SITE)")
        return 2
    checks = run_checks(UrllibHttp(base), site_id=site, username=user, password=pwd)
    print(format_report(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
