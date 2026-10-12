"""The Teltonika listener over TLS (plan C-2).

Trackers dial in over the mobile network, and the listener was plain TCP, so every fix crossed
that network readable. FMB firmware can send over TLS; with a certificate and key configured the
listener now serves TLS, and a client that does not speak it gets nothing.
"""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from minemonitor.config import Settings
from minemonitor.ingest.adapters.teltonika import (
    IMEI_ACCEPT,
    TeltonikaServer,
    ack_bytes,
    tls_context,
)
from tests.test_teltonika import _FakePublisher, _FakeWriter, _frame, _handshake, _record_c8

IMEI = "356307042441013"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


@pytest.fixture
def cert(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for 127.0.0.1, and its key."""
    crt, key = tmp_path / "server.crt", tmp_path / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(crt),
        ],
        check=True,
        capture_output=True,
    )
    return str(crt), str(key)


async def _exchange(
    tls: ssl.SSLContext | None, client: ssl.SSLContext | None, payload: bytes
) -> tuple[bytes, _FakePublisher]:
    """Serve on a free port, send ``payload`` as a tracker would, return what came back."""
    pub = _FakePublisher()
    listener = TeltonikaServer(pub, host="127.0.0.1", port=0, site_id="kn-zw-01", tls=tls)
    server = await listener.start()
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client)
        writer.write(payload)
        await writer.drain()
        try:
            reply = await asyncio.wait_for(reader.read(5), timeout=5)
        except (ConnectionError, ssl.SSLError):
            reply = b""
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    return reply, pub


@needs_openssl
def test_a_tracker_over_tls_is_accepted_and_its_fix_published(cert: tuple[str, str]) -> None:
    client = ssl.create_default_context(cafile=cert[0])  # verifies the listener's certificate
    reply, pub = asyncio.run(
        _exchange(tls_context(*cert), client, _handshake(IMEI) + _frame(0x08, [_record_c8({})]))
    )
    assert reply == IMEI_ACCEPT + ack_bytes(1)
    assert [p.asset_id for p in pub.published] == [f"teltonika-{IMEI}"]


@needs_openssl
def test_a_plain_tcp_client_gets_nothing_from_a_tls_listener(cert: tuple[str, str]) -> None:
    reply, pub = asyncio.run(
        _exchange(tls_context(*cert), None, _handshake(IMEI) + _frame(0x08, [_record_c8({})]))
    )
    assert IMEI_ACCEPT not in reply
    assert pub.published == []


@needs_openssl
def test_the_listener_speaks_nothing_older_than_tls_1_2(cert: tuple[str, str]) -> None:
    ctx = tls_context(*cert)
    assert ctx is not None and ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_NONE  # trackers authenticate by IMEI, not a certificate


def test_plain_tcp_unless_both_certificate_and_key_are_set() -> None:
    assert tls_context("", "") is None
    with pytest.raises(ValueError, match="together"):
        tls_context("/run/teltonika-tls/server.crt", "")
    with pytest.raises(ValueError, match="together"):
        tls_context("", "/run/teltonika-tls/server.key")


def test_a_missing_certificate_stops_the_listener_at_startup(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        tls_context(str(tmp_path / "server.crt"), str(tmp_path / "server.key"))


@needs_openssl
def test_the_settings_name_the_listener_certificate(cert: tuple[str, str]) -> None:
    s = Settings(teltonika_tls_cert=cert[0], teltonika_tls_key=cert[1])
    assert tls_context(s.teltonika_tls_cert, s.teltonika_tls_key) is not None
    assert Settings().teltonika_tls_cert == Settings().teltonika_tls_key == ""


def test_a_connection_reset_mid_frame_is_closed_quietly() -> None:
    # Over TLS a dropped link surfaces as ConnectionResetError or ssl.SSLError, not EOF.
    async def run() -> bool:
        reader = asyncio.StreamReader()
        reader.feed_data(_handshake(IMEI) + b"\x00\x00\x00\x00")
        reader.set_exception(ConnectionResetError("reset by peer"))
        writer = _FakeWriter()
        listener = TeltonikaServer(_FakePublisher(), host="127.0.0.1", port=0, site_id="s")
        await listener._handle(reader, writer)
        return writer.closed

    assert asyncio.run(run())
