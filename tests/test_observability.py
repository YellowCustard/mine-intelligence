"""Request correlation id + safe error boundary (brief §25, §3)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.api.main import create_app
from minemonitor.logging_config import RequestIdFilter, request_id_var, sanitize_request_id
from minemonitor.storage.db import get_db
from tests.conftest import make_client


def test_response_carries_a_request_id(db_session: Session) -> None:
    anon = make_client(db_session, None)
    r = anon.get("/healthz")
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8


def test_valid_client_request_id_is_echoed(db_session: Session) -> None:
    anon = make_client(db_session, None)
    r = anon.get("/healthz", headers={"X-Request-ID": "trace-abc.123"})
    assert r.headers.get("X-Request-ID") == "trace-abc.123"


def test_malicious_request_id_is_replaced() -> None:
    # A header with newlines / control chars must not reach the log verbatim.
    assert sanitize_request_id("bad\nvalue") is None
    assert sanitize_request_id("x" * 100) is None
    assert sanitize_request_id("ok_-.123") == "ok_-.123"


def test_unhandled_error_is_a_clean_500(db_session: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

    @app.get("/_boom")
    def boom() -> None:
        raise RuntimeError("kaboom-secret-internal-detail")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_boom")
    assert r.status_code == 500
    body = r.json()
    # Generic message + correlation id; no traceback or internal detail leaks.
    assert body["detail"] == "internal server error"
    assert body["request_id"] == r.headers.get("X-Request-ID")
    assert "kaboom" not in r.text
    # Security headers still ride the error response.
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_filter_binds_current_request_id() -> None:
    import logging

    rec = logging.LogRecord("t", logging.INFO, "f", 1, "m", (), None)
    token = request_id_var.set("rid-42")
    try:
        assert RequestIdFilter().filter(rec) is True
        assert rec.request_id == "rid-42"  # type: ignore[attr-defined]
    finally:
        request_id_var.reset(token)
