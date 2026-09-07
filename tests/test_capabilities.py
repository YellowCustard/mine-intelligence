"""Role capabilities — the UI hint the dashboard keys role-specific views on."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tests.conftest import ADMIN, SUPERVISOR, VIEWER, make_client


def test_viewer_capabilities(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    caps = c.get("/me/capabilities").json()
    assert caps["role"] == "viewer"
    assert caps["can"]["view_operations"] is True
    assert caps["can"]["acknowledge_alarms"] is False
    assert caps["can"]["configure_site"] is False


def test_supervisor_capabilities(db_session: Session) -> None:
    c = make_client(db_session, SUPERVISOR)
    caps = c.get("/me/capabilities").json()
    assert caps["can"]["acknowledge_alarms"] is True
    assert caps["can"]["manage_incidents"] is True
    assert caps["can"]["configure_site"] is False  # admin-only


def test_admin_capabilities(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    caps = c.get("/me/capabilities").json()
    assert all(caps["can"].values())  # admin can do everything


def test_capabilities_requires_auth(db_session: Session) -> None:
    assert make_client(db_session, None).get("/me/capabilities").status_code == 401
