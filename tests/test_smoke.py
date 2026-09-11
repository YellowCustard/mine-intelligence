"""The post-deploy smoke test, exercised against the in-process app (brief §42)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.smoke import Auth, Resp, format_report, run_checks
from tests.conftest import ADMIN, make_client


class _TCHttp:
    """Adapt a FastAPI TestClient to the smoke Http protocol (per-call auth)."""

    def __init__(self, tc: TestClient) -> None:
        self.tc = tc

    def _resp(self, r: Any) -> Resp:
        try:
            body: Any = r.json()
        except ValueError:
            body = None
        return Resp(status=r.status_code, body=body, text=r.text)

    def get(self, path: str, *, auth: Auth = None) -> Resp:
        return self._resp(self.tc.get(path, auth=auth))

    def post(self, path: str, body: dict[str, Any], *, auth: Auth = None) -> Resp:
        return self._resp(self.tc.post(path, json=body, auth=auth))


def test_smoke_all_checks_pass(db_session: Session) -> None:
    http = _TCHttp(make_client(db_session, None))  # no client-level auth
    checks = run_checks(http, site_id="kn-zw-01", username=ADMIN[0], password=ADMIN[1])
    failed = [c for c in checks if not c.ok]
    assert not failed, format_report(checks)
    assert {c.name for c in checks} == {
        "version",
        "health",
        "auth-enforced",
        "login",
        "ingest-roundtrip",
        "dashboard",
    }


def test_smoke_flags_bad_credentials(db_session: Session) -> None:
    http = _TCHttp(make_client(db_session, None))
    checks = run_checks(http, site_id="kn-zw-01", username=ADMIN[0], password="wrong-password")
    by = {c.name: c for c in checks}
    # Public checks still pass; the authenticated ones fail — overall NOT ok.
    assert by["version"].ok and by["health"].ok and by["auth-enforced"].ok
    assert not by["login"].ok
    assert not all(c.ok for c in checks)
