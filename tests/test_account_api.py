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


def _site_admin(db_session: Session) -> TestClient:
    create_user(
        db_session, username="site-admin", password="password123", role="admin", site_id="kn-zw-01"
    )
    db_session.commit()
    return make_client(db_session, ("site-admin", "password123"))


def test_site_admin_cannot_mint_global_account(db_session: Session) -> None:
    scoped = _site_admin(db_session)
    r = scoped.post(
        "/users",
        json={"username": "root2", "password": "password123", "role": "admin", "site_id": None},
    )
    assert r.status_code == 403
    assert db_session.get(User, "root2") is None


def test_site_admin_cannot_create_user_for_another_site(db_session: Session) -> None:
    scoped = _site_admin(db_session)
    r = scoped.post(
        "/users",
        json={"username": "x", "password": "password123", "role": "viewer", "site_id": "other"},
    )
    assert r.status_code == 403


def test_site_admin_cannot_overwrite_global_admin(db_session: Session) -> None:
    # Regression: "create or replace" let a site admin reset the global admin's password.
    scoped = _site_admin(db_session)
    r = scoped.post(
        "/users",
        json={
            "username": "admin",
            "password": "attacker-pass",
            "role": "admin",
            "site_id": "kn-zw-01",
        },
    )
    assert r.status_code == 403
    assert make_client(db_session, ("admin", "attacker-pass")).get("/me").status_code == 401
    assert make_client(db_session, ("admin", "testpass123")).get("/me").json()["site_id"] is None


def test_site_admin_manages_own_site_accounts(db_session: Session) -> None:
    scoped = _site_admin(db_session)
    body = {"username": "op1", "password": "password123", "role": "viewer", "site_id": "kn-zw-01"}
    assert scoped.post("/users", json=body).status_code == 201
    # Replacing an account already in their site is allowed (password reset).
    assert scoped.post("/users", json={**body, "password": "password456"}).status_code == 201
    assert make_client(db_session, ("op1", "password456")).get("/me").status_code == 200


def test_denied_user_change_is_audited(db_session: Session) -> None:
    from sqlalchemy import select

    from minemonitor.storage.models import AuditLog

    scoped = _site_admin(db_session)
    scoped.post(
        "/users",
        json={"username": "root2", "password": "password123", "role": "admin", "site_id": None},
    )
    actions = db_session.execute(select(AuditLog.action)).scalars().all()
    assert "user.create_denied" in actions
