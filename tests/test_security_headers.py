"""Defence-in-depth response headers (brief §30)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tests.conftest import ADMIN, make_client

_EXPECTED = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def test_security_headers_on_public_endpoint(db_session: Session) -> None:
    anon = make_client(db_session, None)
    r = anon.get("/healthz")
    for name, value in _EXPECTED.items():
        assert r.headers.get(name) == value
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_security_headers_on_dashboard(db_session: Session) -> None:
    # The dashboard is served authenticated; headers must ride it too.
    c = make_client(db_session, ADMIN)
    r = c.get("/")
    assert r.status_code == 200
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    # The CSP must still permit the dashboard's own inline script/style.
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src 'self' 'unsafe-inline'" in csp


def test_security_headers_even_on_401(db_session: Session) -> None:
    # An unauthenticated protected route still gets hardened headers.
    anon = make_client(db_session, None)
    r = anon.get("/sites/kn-zw-01/state")
    assert r.status_code == 401
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
