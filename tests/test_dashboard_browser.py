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
import re
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
    s.flush()  # Asset sorts before Site: write the site first
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
    # First-party vision: a configured camera with a fresh rollup, a dark-camera assessment and
    # a vision alarm whose provenance must be rendered beside its source.
    from datetime import timedelta

    from minemonitor.storage.models import Camera, VisionCameraHealth, VisionZoneRollup

    s.add(
        Camera(
            id="cam-smoke",
            site_id=_SITE,
            name="CAM-SMOKE-01",
            image_zones=[
                {
                    "zone_id": "face",
                    "zone_version": "v1",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                }
            ],
            created_at=now,
            updated_at=now,
        )
    )
    bucket = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    for zone_id, peak in ((None, 3), ("face", 2)):
        s.add(
            VisionZoneRollup(
                rollup_id=f"vr-smoke-{zone_id or 'view'}",
                site_id=_SITE,
                node_id="edge-smoke",
                camera_id="cam-smoke",
                zone_id=zone_id,
                ts=bucket,
                received_at=now,
                bucket_s=300,
                object_class="person",
                count_max=peak,
                count_mean=1.0,
                entries=peak,
                exits=0,
                dwell_s_max=30.0,
                observed_s=120.0,
            )
        )
    s.add(
        VisionCameraHealth(
            health_id="vch-smoke",
            site_id=_SITE,
            node_id="edge-smoke",
            camera_id="cam-smoke",
            ts=now,
            received_at=now,
            state="dark",
        )
    )
    s.add(
        Event(
            event_id="evt-vision-smoke",
            site_id=_SITE,
            ts=now,
            type="zone_breach",
            severity="critical",
            zone_id="face",
            source="vision:cam-smoke",
            summary="Person in restricted zone Pit Face (CAM-SMOKE-01)",
            detail={"provenance": "inferred", "confidence": 0.8},
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
    yield from _serve(db_url)


def _serve(db_url: str) -> Iterator[str]:
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
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.goto(live_server + "/", wait_until="networkidle")

        # The Operations command centre is the default view: headline KPIs render,
        # and the exception layer surfaces the open critical alarm.
        expect(page.locator("#cc-kpis")).to_contain_text("Active equipment", timeout=10_000)
        expect(page.locator("#exbar")).to_contain_text("critical", timeout=10_000)
        # The operational-data-health chip is populated with a real verdict
        # (healthy / degraded / unknown), not the initial placeholder.
        expect(page.locator("#hdr-health")).to_contain_text(
            re.compile("healthy|degraded|unknown"), timeout=10_000
        )
        # The queue KPI never presents degraded analytics silently: a data-confidence
        # badge is always shown (High / Reduced / Low / Unknown).
        expect(page.locator("#cc-queue-conf")).to_contain_text("Data confidence", timeout=10_000)

        # Switch to the Fleet view: the live map data (zone, asset, alarm) renders.
        page.locator("#n-site").click()
        expect(page.locator("#ztab")).to_contain_text("Pit Face", timeout=10_000)
        expect(page.locator("#ftab")).to_contain_text("HT-102", timeout=10_000)
        expect(page.locator("#atab")).to_contain_text("magazine", timeout=10_000)

        # Vision panel: the camera, its condition (dark ≠ empty), people per zone; and the vision
        # alarm carries its provenance beside the source — a camera inference, not a measurement.
        expect(page.locator("#vistab")).to_contain_text("CAM-SMOKE-01", timeout=10_000)
        expect(page.locator("#vistab")).to_contain_text("Dark", timeout=10_000)
        expect(page.locator("#vistab")).to_contain_text("Pit Face", timeout=10_000)
        expect(page.locator("#atab")).to_contain_text("vision:cam-smoke · inferred", timeout=10_000)

        # Machine drill-down: selecting the asset opens its detail without leaving
        # the operational view (identity + investigation sections render).
        page.locator("#ftab tr").first.click()
        expect(page.locator("#machine-detail")).to_contain_text("HT-102", timeout=10_000)
        expect(page.locator("#machine-detail")).to_contain_text("Recent cycles", timeout=10_000)

        # Historical investigation: replay this machine's stored track on the map.
        page.locator("#machine-detail button", has_text="This shift").click()
        expect(page.locator("#md-hist-msg")).to_contain_text("fix(es)", timeout=10_000)

        # Map/table filters: filtering to offline hides the moving asset.
        page.select_option("#flt-state", "offline")
        expect(page.locator("#ftab")).to_contain_text("No machines match", timeout=10_000)
        page.select_option("#flt-state", "all")
        expect(page.locator("#ftab")).to_contain_text("HT-102", timeout=10_000)

        # Analytics view: the trend, bottleneck and downtime panels render (here
        # with first-class empty states, since the smoke seed has no cycles).
        page.locator("#n-analytics").click()
        expect(page.locator("#an-trend")).to_contain_text("No completed cycles", timeout=10_000)
        expect(page.locator("#an-bottlenecks")).to_contain_text(
            "No bottleneck observations", timeout=10_000
        )
        expect(page.locator("#an-downtime")).to_contain_text("No downtime", timeout=10_000)

        # Handover: an admin (supervisor+) sees the record form and can submit; the
        # new handover then appears in the list.
        page.locator("#n-handover").click()
        expect(page.locator("#ho-create")).to_contain_text("Record handover", timeout=10_000)
        page.locator("#ho-notes").fill("day crew: loader B intermittent")
        page.locator("#ho-create button").click()
        expect(page.locator("#ho-list")).to_contain_text("loader B intermittent", timeout=10_000)

        # Reports centre: the current shift report links render (open in new tab).
        page.locator("#n-reports").click()
        expect(page.locator("#rp-shift")).to_contain_text("Current shift", timeout=10_000)
        expect(page.locator("#rp-shift a", has_text="View").first).to_have_attribute(
            "href", re.compile(r"/reports/shift\.html")
        )

        # System view: platform-vs-field health and data-quality drill-down render,
        # and the build/schema version is surfaced for multi-site support (brief §38).
        page.locator("#n-system").click()
        expect(page.locator("#sy-health")).to_contain_text("Verdict", timeout=10_000)
        expect(page.locator("#sy-dq")).to_contain_text("Confidence", timeout=10_000)
        expect(page.locator("#sy-version")).to_contain_text("Mine Monitor v", timeout=10_000)
        # An admin gets the compliance pack in one click, as a zip.
        with page.expect_download() as dl:
            page.locator("#sy-pack").click()
        assert dl.value.suggested_filename.startswith(f"compliance-pack-{_SITE}-")
        assert dl.value.suggested_filename.endswith(".zip")

        # Back to Fleet to acknowledge the alarm.
        page.locator("#n-site").click()
        expect(page.locator("#atab")).to_contain_text("magazine", timeout=10_000)

        # Acknowledge the alarm in the UI; the button then clears on the next poll.
        # Use a retrying locator assertion (not wait_for_function) so the check does
        # not rely on page-side eval, which the production CSP correctly forbids.
        # Target the GNSS alarm's row explicitly — the seed also holds an open vision alarm.
        gnss_row = page.locator("#atab tr", has_text="magazine")
        gnss_row.locator(".ackbtn").click()
        expect(gnss_row.locator(".ackbtn")).to_have_count(0, timeout=15_000)

        assert page_errors == [], page_errors  # no JS errors anywhere in the walk-through
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


# Strings that would execute if any sink interpolated them raw: an HTML payload, and a
# JS-string breakout aimed at the inline onclick handlers.
_XSS_ASSET = "<img src=x onerror=window.__xss=1>"
_XSS_BREAKOUT = "x');window.__xss=2;('"


def _seed_hostile(db_url: str) -> None:
    """Rows that bypass the ingest contract (legacy data, a direct DB write, a future
    source with weaker validation). The dashboard must render them as inert text."""
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from minemonitor.auth.service import create_user
    from minemonitor.storage.models import Base, Event, Position, Site
    from minemonitor.zones.geometry import box_polygon
    from minemonitor.zones.repository import upsert_zone

    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    now = datetime.now(UTC)
    lat, lon = -17.8252, 31.0335
    s.add(Site(site_id=_SITE, name="Hostile Site", timezone="Africa/Harare"))
    s.commit()
    upsert_zone(
        s,
        site_id=_SITE,
        zone_id="<b>z",
        name="<img src=x onerror=window.__xss=3>",
        kind="restricted",
        geometry=box_polygon(lat, lon, 120),
        rules={"authorized_classes": ["<img src=x onerror=window.__xss=4>"]},
    )
    for i, asset_id in enumerate((_XSS_ASSET, _XSS_BREAKOUT)):
        s.add(
            Position(
                site_id=_SITE,
                asset_id=asset_id,
                ts=now,
                received_at=now,
                lat=lat + i * 0.0005,
                lon=lon,
                speed_kph=8.0,
                ignition=True,
                source="seed",
            )
        )
        s.add(
            Event(
                event_id=f"evt-{i}');window.__xss=5;('",
                site_id=_SITE,
                ts=now,
                type="zone_breach",
                severity="critical",
                asset_id=asset_id,
                zone_id="<b>z",
                source="<img src=x onerror=window.__xss=6>",
                summary="<img src=x onerror=window.__xss=7>",
                advisory=True,
                state="open",
            )
        )
    create_user(s, username=_ADMIN[0], password=_ADMIN[1], role="admin")
    s.commit()
    s.close()
    engine.dispose()


@pytest.fixture
def hostile_server(tmp_path: Path) -> Iterator[str]:
    db_url = f"sqlite:///{tmp_path / 'hostile.db'}"
    _seed_hostile(db_url)
    yield from _serve(db_url)


def test_dashboard_renders_hostile_strings_inert(hostile_server: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium_executable())
        context = browser.new_context(
            http_credentials={"username": _ADMIN[0], "password": _ADMIN[1]}
        )
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.goto(hostile_server + "/", wait_until="networkidle")
        page.locator("#n-site").click()
        # The markup renders as literal text in the fleet and alarm tables.
        expect(page.locator("#ftab")).to_contain_text(_XSS_ASSET, timeout=10_000)
        expect(page.locator("#ftab")).to_contain_text(_XSS_BREAKOUT, timeout=10_000)
        expect(page.locator("#atab")).to_contain_text("onerror=window.__xss=7", timeout=10_000)
        expect(page.locator("#ztab")).to_contain_text("onerror=window.__xss=3", timeout=10_000)
        # Exercise the inline handlers that carry ids: select each machine (fleet row
        # onclick), open its history buttons, and acknowledge an alarm.
        for asset_id in (_XSS_ASSET, _XSS_BREAKOUT):
            page.locator("#ftab tr", has_text=asset_id).click()
            expect(page.locator("#machine-detail")).to_contain_text(asset_id, timeout=10_000)
            page.locator("#machine-detail button", has_text="Last 1h").click()
        page.locator("#atab .ackbtn").first.click()
        page.wait_for_timeout(1500)
        assert page.evaluate("window.__xss") is None
        # A handler that throws on an escaped argument would also leave __xss unset;
        # require that the handlers actually ran cleanly.
        assert page_errors == []
        browser.close()
