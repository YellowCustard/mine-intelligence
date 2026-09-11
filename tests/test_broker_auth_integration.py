"""Authenticated-broker integration test (brief §10/§11).

Proves the *transport* layer end to end: the DB-driven password + ACL files render
credentials a real Mosquitto accepts, the broker rejects a bad password, and the ACL
confines a device to its own topic. This is the correctness gate for the pure-Python
``$7$`` hash — if the format were wrong, a real broker would refuse it here.

Runs whenever a ``mosquitto`` binary is on PATH (CI installs it; most dev boxes have
it). Uses the project's own paho dependency.
"""

from __future__ import annotations

import getpass
import shutil
import socket
import subprocess
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from sqlalchemy.orm import Session

from minemonitor.devices import service
from minemonitor.devices.acl import render_mosquitto_acl
from minemonitor.devices.broker_config import mosquitto_password_hash, render_password_file

pytestmark = pytest.mark.skipif(
    shutil.which("mosquitto") is None, reason="mosquitto binary not installed"
)

_SVC_USER = "mm-ingestor"
_SVC_PASS = "svc-secret-pass"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_broker(tmp_path: Path, port: int, passwd: str, acl: str) -> subprocess.Popen[bytes]:
    (tmp_path / "passwd").write_text(passwd)
    (tmp_path / "acl").write_text(acl)
    conf = tmp_path / "mosquitto.conf"
    # Pin to the current user so mosquitto (which otherwise drops from root to the
    # 'mosquitto' user) can still read these run-owned temp files, in CI and locally.
    conf.write_text(
        f"listener {port} 127.0.0.1\n"
        "allow_anonymous false\n"
        f"user {getpass.getuser()}\n"
        f"password_file {tmp_path / 'passwd'}\n"
        f"acl_file {tmp_path / 'acl'}\n"
    )
    proc = subprocess.Popen(["mosquitto", "-c", str(conf)])
    # Wait for the port to accept connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    return proc


def _connect(port: int, user: str, password: str, cid: str) -> tuple[mqtt.Client, int]:
    """Connect and return (client, connack reason code). 0 = accepted, 5 = not authorized."""
    got: dict[str, int] = {}
    done = __import__("threading").Event()

    def on_connect(c, u, flags, reason_code, properties=None) -> None:
        got["rc"] = int(reason_code.value if hasattr(reason_code, "value") else reason_code)
        done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    client.username_pw_set(user, password)
    client.on_connect = on_connect
    client.connect_async("127.0.0.1", port)
    client.loop_start()
    done.wait(timeout=5)
    return client, got.get("rc", -1)


def test_real_broker_accepts_rendered_credentials_and_enforces_acl(
    db_session: Session, tmp_path: Path
) -> None:
    # DB-driven credentials: a real device with an issued secret + the service account.
    service.register_device(db_session, device_id="trk-1", site_id="kn-zw-01", asset_id="HT-102")
    device_secret = service.rotate_secret(db_session, "trk-1")
    db_session.commit()
    passwd = render_password_file(
        db_session, service_user=_SVC_USER, service_hash=mosquitto_password_hash(_SVC_PASS)
    )
    acl = render_mosquitto_acl(db_session, ingest_user=_SVC_USER)

    port = _free_port()
    broker = _start_broker(tmp_path, port, passwd, acl)
    clients: list[mqtt.Client] = []
    try:
        # 1) Service account authenticates (proves the $7$ hash is broker-valid).
        svc, rc = _connect(port, _SVC_USER, _SVC_PASS, "svc")
        clients.append(svc)
        assert rc == 0

        # 2) Wrong password is refused (5 = not authorized).
        bad, rc_bad = _connect(port, _SVC_USER, "wrong", "bad")
        clients.append(bad)
        assert rc_bad != 0

        # 3) Device authenticates with its issued secret.
        dev, rc_dev = _connect(port, "trk-1", device_secret, "trk-1")
        clients.append(dev)
        assert rc_dev == 0

        # 4) ACL: the service subscribes; the device may publish its own topic but not
        #    another asset's. The foreign publish is dropped by the broker (ACL deny).
        received: list[str] = []
        svc.on_message = lambda c, u, msg: received.append(msg.topic)
        svc.subscribe("mm/#", qos=1)
        time.sleep(0.5)
        dev.publish("mm/kn-zw-01/HT-102/position", b"{}", qos=1).wait_for_publish(timeout=5)
        dev.publish("mm/kn-zw-01/OTHER/position", b"{}", qos=1)  # ACL-denied
        time.sleep(1.0)
        assert "mm/kn-zw-01/HT-102/position" in received
        assert "mm/kn-zw-01/OTHER/position" not in received
    finally:
        for c in clients:
            c.loop_stop()
            c.disconnect()
        broker.terminate()
        broker.wait(timeout=5)
