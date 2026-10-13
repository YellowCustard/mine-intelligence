"""WhatsApp alarms go out as an approved template, so they reach a supervisor at any hour.

WhatsApp delivers a free-form text message only inside a customer service window, the 24 hours
after the recipient last wrote to the business number. Outside it, "template messages are the
only type of message that can be sent". A supervisor who gets alarms does not message the mine's
number every day, so alarms sent as plain text did not reach them. Alarms are now sent as the
approved template named in ``MM_NOTIFY_WHATSAPP_TEMPLATE``, with severity, site and summary as
its body parameters, and production refuses to start with WhatsApp on and no template.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from minemonitor.config import Settings
from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.notifications import outbox
from minemonitor.notifications.dispatch import UrllibSmtpSender, dispatch_pending

_NOW = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
SITE = "kn-zw-01"
_SAFE_DB = "postgresql+psycopg://mmuser:a-strong-secret@db.internal:5432/minemonitor"


class _CloudApi(http.server.BaseHTTPRequestHandler):
    """Stands in for the Cloud API messages endpoint: records each request body, accepts it."""

    bodies: list[dict[str, Any]] = []
    auth: list[str | None] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        length = int(self.headers.get("Content-Length", "0"))
        _CloudApi.bodies.append(json.loads(self.rfile.read(length)))
        _CloudApi.auth.append(self.headers.get("Authorization"))
        reply = json.dumps({"messages": [{"id": "wamid.test"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def cloud_api() -> Iterator[str]:
    _CloudApi.bodies, _CloudApi.auth = [], []
    server = http.server.HTTPServer(("127.0.0.1", 0), _CloudApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v20.0/1234/messages"
    finally:
        server.shutdown()
        server.server_close()


def _settings(url: str, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "notify_min_severity": "warning",
        "notify_whatsapp_url": url,
        "notify_whatsapp_token": "tok-123",
        "notify_whatsapp_to": "+263771234567",
        "notify_whatsapp_template": "mm_alarm",
        "notify_whatsapp_template_lang": "en",
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _send(db: Session, s: Settings, summary: str) -> dict[str, int]:
    ev = EventV1(
        event_id="wa-t1",
        site_id=SITE,
        ts=_NOW,
        type="zone_breach",
        severity="critical",
        asset_id="LV-07",
        zone_id="r1-explosives-magazine",
        source="gnss_geofence",
        summary=summary,
    )
    persist_event(db, ev)
    outbox.enqueue_for_event(db, ev, settings=s, now=_NOW)
    db.commit()
    return dispatch_pending(db, sender=UrllibSmtpSender(s, timeout_s=5), settings=s, now=_NOW)


def test_an_alarm_is_sent_as_the_approved_template(db_session: Session, cloud_api: str) -> None:
    s = _settings(cloud_api)
    assert _send(db_session, s, "LV-07 entered R1 Explosives Magazine")["sent"] == 1

    assert _CloudApi.auth == ["Bearer tok-123"]
    (body,) = _CloudApi.bodies
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "+263771234567"
    assert body["type"] == "template"
    assert "text" not in body
    template = body["template"]
    assert template["name"] == "mm_alarm"
    assert template["language"] == {"code": "en"}
    (component,) = template["components"]
    assert component["type"] == "body"
    assert component["parameters"] == [
        {"type": "text", "text": "critical"},
        {"type": "text", "text": SITE},
        {"type": "text", "text": "LV-07 entered R1 Explosives Magazine"},
    ]


def test_template_parameters_carry_no_line_breaks_or_runs_of_spaces(
    db_session: Session, cloud_api: str
) -> None:
    # A zone name typed with a line break or a tab must not make WhatsApp refuse the alarm.
    _send(db_session, _settings(cloud_api), "LV-07 entered\nR1\tExplosives     Magazine")
    params = _CloudApi.bodies[0]["template"]["components"][0]["parameters"]
    assert params[2]["text"] == "LV-07 entered R1 Explosives Magazine"


def test_without_a_template_outside_production_it_sends_plain_text(
    db_session: Session, cloud_api: str
) -> None:
    # For trying the channel out from a phone that has just written to the number.
    s = _settings(cloud_api, notify_whatsapp_template="")
    assert _send(db_session, s, "LV-07 entered R1 Explosives Magazine")["sent"] == 1
    (body,) = _CloudApi.bodies
    assert body["type"] == "text"
    assert body["text"]["body"] == (
        f"[critical] {SITE}: LV-07 entered R1 Explosives Magazine (advisory)"
    )


def test_production_refuses_whatsapp_without_a_template() -> None:
    prod: dict[str, Any] = {
        "env": "prod",
        "database_url": _SAFE_DB,
        "mqtt_username": "mm-ingestor",
        "notify_whatsapp_url": "https://graph.facebook.com/v20.0/1234/messages",
        "notify_whatsapp_token": "tok",
        "notify_whatsapp_to": "+263771234567",
    }
    with pytest.raises(ValueError, match="MM_NOTIFY_WHATSAPP_TEMPLATE"):
        Settings(_env_file=None, **prod)  # type: ignore[arg-type]
    ok = Settings(_env_file=None, **prod, notify_whatsapp_template="mm_alarm")  # type: ignore[arg-type]
    assert ok.notify_whatsapp_template == "mm_alarm"
    # WhatsApp off (no recipients): no template needed.
    off = {**prod, "notify_whatsapp_to": ""}
    assert Settings(_env_file=None, **off).env == "prod"  # type: ignore[arg-type]
