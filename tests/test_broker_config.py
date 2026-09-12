"""Mosquitto credential rendering: $7$ hash format + password/ACL files (§10/§11)."""

from __future__ import annotations

import base64
import hashlib

from sqlalchemy.orm import Session

from minemonitor.devices import service
from minemonitor.devices.broker_config import (
    mosquitto_password_hash,
    render_password_file,
)


def test_hash_has_mosquitto_pbkdf2_sha512_shape() -> None:
    h = mosquitto_password_hash("s3cret", iterations=1000, salt=b"0123456789ab")
    scheme, iters, b64_salt, b64_hash = h.split("$")[1:]
    assert scheme == "7"
    assert iters == "1000"
    assert base64.b64decode(b64_salt) == b"0123456789ab"
    # The stored hash is exactly PBKDF2-HMAC-SHA512(password, salt, iters, 64 bytes).
    expected = hashlib.pbkdf2_hmac("sha512", b"s3cret", b"0123456789ab", 1000, dklen=64)
    assert base64.b64decode(b64_hash) == expected


def test_hash_salt_is_random_per_call() -> None:
    assert mosquitto_password_hash("x") != mosquitto_password_hash("x")


def test_password_file_has_service_account_and_only_credentialled_enabled_devices(
    db_session: Session,
) -> None:
    # trk-1: enabled with a secret -> present. trk-2: enabled, no secret -> absent.
    # trk-3: has a secret but disabled -> absent.
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    service.register_device(db_session, device_id="trk-2", site_id="kn-zw-01", asset_id="EX-01")
    service.register_device(db_session, device_id="trk-3", site_id="kn-zw-01", asset_id="LV-07")
    service.rotate_secret(db_session, "trk-1")
    service.rotate_secret(db_session, "trk-3")
    service.set_enabled(db_session, "trk-3", False)
    db_session.commit()

    txt = render_password_file(
        db_session, service_user="mm-ingestor", service_hash=mosquitto_password_hash("svc")
    )
    lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("#")]
    users = {ln.split(":", 1)[0] for ln in lines}
    assert users == {"mm-ingestor", "trk-1"}
    # No cleartext secret ever appears in the file — only $7$ hashes.
    assert all(ln.split(":", 1)[1].startswith("$7$") for ln in lines)


def test_rotate_secret_returns_cleartext_and_stores_only_hash(db_session: Session) -> None:
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    secret = service.rotate_secret(db_session, "trk-1")
    dev = service.get_device(db_session, "trk-1")
    assert dev is not None and dev.broker_pw_hash is not None
    assert secret not in dev.broker_pw_hash  # cleartext is never stored
    assert dev.broker_pw_hash.startswith("$7$")
    # Rotating again issues a different secret and hash.
    assert service.rotate_secret(db_session, "trk-1") != secret
