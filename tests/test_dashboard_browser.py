"""End-to-end dashboard smoke test in a real browser (Phase 3).

Gated: set ``MM_TEST_BROWSER=1`` and install the ``browser`` extra
(``uv sync --extra browser``; a Chromium build must be available to Playwright).
It starts a real uvicorn server on a seeded SQLite database, loads the dashboard
in Chromium with HTTP Basic auth, and confirms the live data path end to end:
zones/assets/alarms fetched from the API render, and acknowledging an alarm in
the UI moves it out of the open state.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MM_TEST_BROWSER") != "1",
    reason="set MM_TEST_BROWSER=1 and install the 'browser' extra to run the dashboard smoke test",
)

_ADMIN = ("dash-admin", "dash-password-1")
_SITE = "kn-zw-01"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(db_url: str) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from minemonitor.auth.service import create_user
    from minemonitor.storage.models import Asset, Base, Event, Position, Site
    from minemonitor.zones.geometry import box_polygon
    from minemonitor.zones.repository import upsert_zone

    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    now = datetime.now(UTC)
    lat, lon = -17.8252, 31.0335
    s.add(Site(site_id=_SITE, name="Smoke Site", timezone="Africa/Harare"))
    s.add(Asset(asset_id="HT-102", site_id=_SITE, asset_class="haul_truck"))
    s.commit()
    upsert_zone(
        s,
        site_id=_SITE,
        zone_id="face",
        name="Pit Face",
        kind="loading",
        geometry=box_polygon(lat, lon, 120),
        rules={},
    )
    s.add(
        Position(
            site_id=_SITE,
            asset_id="HT-102",
            ts=now,
            received_at=now,
            lat=lat,
            lon=lon,
            speed_kph=8.0,
            ignition=True,
            source="seed",
        )
    )
    s.add(
        Event(
            event_id="evt-smoke",
            site_id=_SITE,
            ts=now,
            type="zone_breach",
            severity="critical",
            asset_id="HT-102",
            zone_id="face",
            source="gnss_geofence",
            summary="LV in the magazine",
            advisory=True,
            state="open",
        )
    )
    create_user(s, username=_ADMIN[0], password=_ADMIN[1], role="admin")
    s.commit()
    s.close()
    engine.dispose()


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    db_url = f"sqlite:///{tmp_path / 'dash.db'}"
    _seed(db_url)
    port = _free_port()
    env = {
        **os.environ,
        "MM_DATABASE_URL": db_url,
        "MM_MQTT_HOST": "127.0.0.1",
        "MM_BOOTSTRAP_ADMIN_USER": "",
        "MM_BOOTSTRAP_ADMIN_PASSWORD": "",
    }
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "minemonitor.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1)
                break
            except urllib.error.URLError:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not become ready")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _chromium_executable() -> str | None:
    """Prefer an explicit path, else a preinstalled Chromium, else Playwright's own."""
    import glob

    explicit = os.environ.get("MM_TEST_CHROMIUM")
    if explicit:
        return explicit
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return found[-1] if found else None


def test_dashboard_renders_live_data_and_acknowledges(live_server: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium_executable())
        context = browser.new_context(
            http_credentials={"username": _ADMIN[0], "password": _ADMIN[1]}
        )
        page = context.new_page()
        page.goto(live_server + "/", wait_until="networkidle")

        # Live data from the API renders: the seeded zone, fleet asset, and alarm.
        expect(page.locator("#ztab")).to_contain_text("Pit Face", timeout=10_000)
        expect(page.locator("#ftab")).to_contain_text("HT-102", timeout=10_000)
        expect(page.locator("#atab")).to_contain_text("magazine", timeout=10_000)

        # Acknowledge the alarm in the UI; the button then clears on the next poll.
        page.locator("#atab .ackbtn").first.click()
        page.wait_for_function(
            "document.querySelectorAll('#atab .ackbtn').length === 0", timeout=15_000
        )

        browser.close()

    # The acknowledgement reached the API (authenticated user, not the payload).
    import base64
    import json

    token = base64.b64encode(f"{_ADMIN[0]}:{_ADMIN[1]}".encode()).decode()
    req = urllib.request.Request(
        f"{live_server}/sites/{_SITE}/events", headers={"Authorization": f"Basic {token}"}
    )
    events = json.loads(urllib.request.urlopen(req, timeout=5).read())
    evt = next(e for e in events if e["event_id"] == "evt-smoke")
    assert evt["state"] == "acknowledged"
    assert evt["acknowledged_by"] == _ADMIN[0]
