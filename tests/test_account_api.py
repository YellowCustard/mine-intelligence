"""HTTP coverage for the account/admin surface: /me, users, retention (Phase 3)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from minemonitor.auth.service import create_user
from minemonitor.storage.models import User
from tests.conftest import VIEWER, make_client


def test_me_returns_identity(client: TestClient) -> None:
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "role": "admin", "site_id": None}


def test_admin_creates_user_over_api(client: TestClient, db_session: Session) -> None:
    r = client.post(
        "/users", json={"username": "newbie", "password": "password123", "role": "viewer"}
    )
    assert r.status_code == 201
    assert db_session.get(User, "newbie") is not None


def test_create_user_rejects_short_password(client: TestClient) -> None:
    r = client.post("/users", json={"username": "x", "password": "short", "role": "viewer"})
    assert r.status_code == 422  # Pydantic min_length


def test_viewer_cannot_create_user(db_session: Session) -> None:
    viewer = make_client(db_session, VIEWER)
    r = viewer.post("/users", json={"username": "x", "password": "password123", "role": "viewer"})
    assert r.status_code == 403


def test_retention_run_is_global_admin_only(client: TestClient, db_session: Session) -> None:
    # A global admin can trigger it.
    assert client.post("/admin/retention/run").status_code == 200
    # A site-scoped admin cannot (it is a cross-site, global action).
    create_user(
        db_session, username="site-admin", password="password123", role="admin", site_id="kn-zw-01"
    )
    db_session.commit()
    scoped = make_client(db_session, ("site-admin", "password123"))
    assert scoped.post("/admin/retention/run").status_code == 403
