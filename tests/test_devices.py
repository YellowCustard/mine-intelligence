"""Device provisioning: service, API, MQTT topic authz, and the broker ACL (§10/§11)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from minemonitor.devices import service
from minemonitor.devices.acl import render_mosquitto_acl
from minemonitor.ingest.authz import parse_position_topic, topic_matches_payload
from tests.conftest import ADMIN, VIEWER, make_client

# --- service ---------------------------------------------------------------


def test_register_and_authorize(db_session: Session) -> None:
    service.register_device(
        db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102", source="teltonika"
    )
    db_session.commit()
    assert service.authorized_asset(db_session, "kn-zw-01", "HT-102") is True
    assert service.authorized_asset(db_session, "kn-zw-01", "HT-999") is False


def test_one_device_per_asset(db_session: Session) -> None:
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    db_session.commit()
    # A different device cannot claim an already-bound asset.
    try:
        service.register_device(
            db_session, device_id="trk-2", site_id="kn-zw-01", asset_id="HT-102"
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "already bound" in str(exc)


def test_disable_revokes_authorization(db_session: Session) -> None:
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    db_session.commit()
    service.set_enabled(db_session, "trk-1", False)
    db_session.commit()
    assert service.authorized_asset(db_session, "kn-zw-01", "HT-102") is False


# --- MQTT topic authz ------------------------------------------------------


def test_parse_and_match_topic() -> None:
    assert parse_position_topic("mm", "mm/kn-zw-01/HT-102/position") == ("kn-zw-01", "HT-102")
    assert parse_position_topic("mm", "mm/kn-zw-01/position") is None  # malformed
    assert topic_matches_payload("mm", "mm/kn-zw-01/HT-102/position", "kn-zw-01", "HT-102")
    # Body claims a different asset than the topic → rejected.
    assert not topic_matches_payload("mm", "mm/kn-zw-01/HT-102/position", "kn-zw-01", "OTHER")


# --- broker ACL ------------------------------------------------------------


def test_acl_scopes_each_device_to_its_topic(db_session: Session) -> None:
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    service.register_device(db_session, device_id="trk-2", site_id="kn-zw-01", asset_id="EX-01")
    service.set_enabled(db_session, "trk-2", False)  # disabled → not in the ACL
    db_session.commit()
    acl = render_mosquitto_acl(db_session)
    assert "user mm-ingestor" in acl and "topic read mm/#" in acl
    assert "user trk-1" in acl and "topic write mm/kn-zw-01/HT-102/position" in acl
    assert "trk-2" not in acl  # disabled device excluded


# --- API -------------------------------------------------------------------


def test_admin_provisions_and_lists(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    r = c.post(
        "/sites/kn-zw-01/devices",
        json={"device_id": "trk-1", "asset_id": "HT-102", "source": "teltonika"},
    )
    assert r.status_code == 201 and r.json()["enabled"] is True
    listed = c.get("/sites/kn-zw-01/devices").json()
    assert [d["device_id"] for d in listed] == ["trk-1"]


def test_provisioning_returns_secret_once_and_never_leaks_it(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    created = c.post("/sites/kn-zw-01/devices", json={"device_id": "trk-1", "asset_id": "HT-102"})
    body = created.json()
    assert body.get("secret")  # issued once, in the create response
    # Neither list nor read ever return the secret or its hash.
    listed = c.get("/sites/kn-zw-01/devices").json()[0]
    assert "secret" not in listed and "broker_pw_hash" not in listed


def test_rotate_secret_endpoint(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    first = c.post(
        "/sites/kn-zw-01/devices", json={"device_id": "trk-1", "asset_id": "HT-102"}
    ).json()["secret"]
    r = c.post("/sites/kn-zw-01/devices/trk-1/rotate-secret")
    assert r.status_code == 200 and r.json()["secret"] and r.json()["secret"] != first
    # Unknown device / wrong site -> 404.
    assert c.post("/sites/kn-zw-01/devices/nope/rotate-secret").status_code == 404


def test_viewer_cannot_rotate_secret(db_session: Session) -> None:
    make_client(db_session, ADMIN).post(
        "/sites/kn-zw-01/devices", json={"device_id": "trk-1", "asset_id": "HT-102"}
    )
    r = make_client(db_session, VIEWER).post("/sites/kn-zw-01/devices/trk-1/rotate-secret")
    assert r.status_code == 403


def test_duplicate_asset_binding_is_409(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    c.post("/sites/kn-zw-01/devices", json={"device_id": "trk-1", "asset_id": "HT-102"})
    r = c.post("/sites/kn-zw-01/devices", json={"device_id": "trk-2", "asset_id": "HT-102"})
    assert r.status_code == 409


def test_viewer_cannot_provision(db_session: Session) -> None:
    c = make_client(db_session, VIEWER)
    r = c.post("/sites/kn-zw-01/devices", json={"device_id": "x", "asset_id": "HT-102"})
    assert r.status_code == 403


def test_provisioning_is_audited(db_session: Session) -> None:
    c = make_client(db_session, ADMIN)
    c.post("/sites/kn-zw-01/devices", json={"device_id": "trk-1", "asset_id": "HT-102"})
    audit = c.get("/sites/kn-zw-01/audit").json()
    assert any(a["action"] == "device.register" and a["entity_id"] == "trk-1" for a in audit)
