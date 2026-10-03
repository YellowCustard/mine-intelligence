"""Credentialed outbound HTTP never follows redirects.

Regression: ``urllib.request.urlopen`` copies ``Authorization`` onto a redirect even
across origins, and turns a redirected POST into a body-less GET. Each credentialed
client must refuse the redirect, deliver nothing to the redirect target, and surface
the failure through its normal error path.
"""

from __future__ import annotations

import http.server
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from minemonitor import net
from minemonitor.ingest.adapters.dahua_gate import _urllib_get
from minemonitor.notifications.dispatch import UrllibSmtpSender
from minemonitor.vision.edge.node import fetch_edge_config
from minemonitor.vision.edge.publish import HttpPublisher, TransientPublishError


class _Sink(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []

    def _record(self) -> None:
        _Sink.hits.append((self.command, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _record

    def log_message(self, *args: object) -> None:
        pass


def _redirector(target: str, code: int) -> type[http.server.BaseHTTPRequestHandler]:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def _go(self) -> None:
            self.send_response(code)
            self.send_header("Location", target + self.path)
            self.end_headers()

        do_GET = do_POST = do_PUT = _go

        def log_message(self, *args: object) -> None:
            pass

    return Redirect


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(params=[301, 302, 303, 307, 308])
def redirecting(request: pytest.FixtureRequest) -> Iterator[str]:
    """A server on one origin that redirects everything to a sink on another origin."""
    _Sink.hits = []
    sink = _serve(_Sink)
    red = _serve(_redirector(f"http://127.0.0.1:{sink.server_address[1]}", request.param))
    try:
        yield f"http://127.0.0.1:{red.server_address[1]}"
    finally:
        red.shutdown()
        sink.shutdown()


def test_net_urlopen_refuses_redirect(redirecting: str) -> None:
    req = urllib.request.Request(redirecting + "/x", headers={"Authorization": "Basic c2VjcmV0"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        net.urlopen(req, timeout=5)
    assert 300 <= exc.value.code < 400
    assert _Sink.hits == []  # the credential never reached the other origin


def test_net_urlopen_passes_plain_responses() -> None:
    _Sink.hits = []
    sink = _serve(_Sink)
    try:
        url = f"http://127.0.0.1:{sink.server_address[1]}/ok"
        with net.urlopen(urllib.request.Request(url), timeout=5) as resp:
            assert resp.status == 200
    finally:
        sink.shutdown()


def test_edge_publisher_treats_redirect_as_transient(redirecting: str) -> None:
    pub = HttpPublisher(redirecting, "edge-01", "edge-secret")
    with pytest.raises(TransientPublishError):
        pub([{"schema": "vision.health.v1"}])  # never marked sent
    assert _Sink.hits == []


def test_edge_config_fetch_refuses_redirect(redirecting: str) -> None:
    with pytest.raises(urllib.error.URLError):  # the node falls back to its cached config
        fetch_edge_config(redirecting, "kn-zw-01", "edge-01", "edge-secret")
    assert _Sink.hits == []


def test_gate_poll_refuses_redirect(redirecting: str) -> None:
    with pytest.raises(urllib.error.HTTPError):
        _urllib_get(redirecting + "/events", "gate-token", 5)
    assert _Sink.hits == []


def test_webhook_and_whatsapp_refuse_redirect(redirecting: str) -> None:
    settings = SimpleNamespace(
        notify_whatsapp_url=redirecting + "/messages", notify_whatsapp_token="wa-token"
    )
    sender = UrllibSmtpSender(settings, timeout_s=5)  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        sender.webhook(redirecting + "/hook", {"event_id": "e1"})
    with pytest.raises(urllib.error.HTTPError):
        sender.whatsapp("+263000000000", "alarm")
    assert _Sink.hits == []  # a redirect is a failed delivery, retried later — never "sent"
