"""Mosquitto credential rendering: $7$ hash format + password/ACL files (§10/§11)."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from minemonitor.devices import service
from minemonitor.devices.broker_config import (
    BROKER_GID,
    BROKER_UID,
    broker_file_owner,
    mosquitto_password_hash,
    render_password_file,
    write_broker_files,
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


def _write(db_session: Session, out_dir: Path, owner: tuple[int, int] | None) -> tuple[Path, Path]:
    passwd, acl = write_broker_files(
        db_session,
        out_dir=str(out_dir),
        prefix="mm",
        service_user="mm-ingestor",
        service_password="svc",
        owner=owner,
    )
    return Path(passwd), Path(acl)


def test_broker_files_are_private_to_the_given_owner(db_session: Session, tmp_path: Path) -> None:
    owner = (os.getuid(), os.getgid())
    for path in _write(db_session, tmp_path, owner):
        st = path.stat()
        assert stat.S_IMODE(st.st_mode) == 0o600, path
        assert (st.st_uid, st.st_gid) == owner, path
    assert sorted(p.name for p in tmp_path.iterdir()) == ["acl", "passwd"]  # no temp files left


def test_broker_files_are_replaced_whole(db_session: Session, tmp_path: Path) -> None:
    # The broker re-reads both files on SIGHUP, and the site loses power without warning, so a
    # rewrite swaps in a complete new file rather than truncating the one the broker reads.
    old = tmp_path / "passwd"
    old.write_text("stale\n")
    old.chmod(0o644)
    inode = old.stat().st_ino
    passwd, _ = _write(db_session, tmp_path, None)
    assert passwd.stat().st_ino != inode
    assert stat.S_IMODE(passwd.stat().st_mode) == 0o600
    assert "\nmm-ingestor:$7$" in passwd.read_text()


def test_files_go_to_the_broker_user_only_when_running_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The broker drops from root to the image's mosquitto user before it reads these files.
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert broker_file_owner() == (BROKER_UID, BROKER_GID) == (1883, 1883)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert broker_file_owner() is None
