"""Stage C pilot readiness (vision V5): live source, edge node service, evidence, broker identity.

Everything here runs without OpenCV, weights or a camera: the node is driven by fake sources that
hand over simulated frames, the core is the real API (device TestClient), evidence uses a fake
encoder, and the S3 client is checked against AWS's published SigV4 example plus a local server
that re-verifies every signature. The OpenCV-backed pieces (a real decoder reconnecting, JPEG
encoding) are in ``tests/test_vision_yolox.py``; the real broker ACL in
``tests/test_broker_auth_integration.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.contracts.vision import Evidence
from minemonitor.devices import service as devices
from minemonitor.devices.acl import VISION_EDGE_SOURCE, render_mosquitto_acl
from minemonitor.events.repository import list_events
from minemonitor.storage.models import Camera, VisionCameraHealth, VisionNodeHealth
from minemonitor.vision import service
from minemonitor.vision.edge.cli import main as cli_main
from minemonitor.vision.edge.evidence import EvidenceStore
from minemonitor.vision.edge.node import (
    ConfigUnavailableError,
    EdgeNode,
    EdgeSettings,
    camera_fingerprint,
)
from minemonitor.vision.edge.publish import RejectedRecordError, vision_topic
from minemonitor.vision.edge.rtsp import Backoff, RtspSource, redact_url
from minemonitor.vision.edge.runtime import FrameIn
from minemonitor.vision.edge.s3 import EMPTY_SHA256, S3Error, S3Uploader, sign_v4
from minemonitor.vision.sim import START, SimModel, frames
from tests.conftest import DEVICE, make_client
from tests.test_vision_pipeline import NODE, SITE, Published, make_sim_camera

STREAM = "rtsp://admin:s3cr3t-pass@10.20.0.5:554/cam/realmonitor?channel=1&subtype=1"


@pytest.fixture
def cam(db_session: Session) -> Camera:
    return make_sim_camera(db_session)


# -- fakes ---------------------------------------------------------------------------------------


class Clock:
    """One fake time for the node's wall clock and monotonic clock."""

    def __init__(self, t: datetime = START) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def mono(self) -> float:
        return (self.t - START).total_seconds()


class FakeSource:
    """Hands over pre-rendered frames on demand; can be "down" (no frames, not connected)."""

    def __init__(self, frames_: list[FrameIn] | None = None, *, connected: bool = True) -> None:
        self.queue = list(frames_ or [])
        self.connected = connected
        self.dropped = 0
        self.last_frame_at: datetime | None = None
        self.last_error: str | None = None if connected else "stream did not open"
        self.started = self.stopped = False

    def start(self) -> FakeSource:
        self.started = True
        return self

    def stop(self) -> None:
        self.stopped = True

    def latest(self, timeout_s: float = 0.0) -> FrameIn | None:
        if not self.queue:
            return None
        f = self.queue.pop(0)
        self.last_frame_at = f.ts
        return f


def sim_frames(name: str) -> list[FrameIn]:
    return [FrameIn(f.ts, f.frame_id, f, f.camera_state) for f in frames(name)]


def make_node(
    db: Session,
    tmp_path: Path,
    sources: dict[str, FakeSource],
    clock: Clock,
    *,
    fetch: Any = None,
    evidence: EvidenceStore | None = None,
    publisher: Published | None = None,
    **overrides: Any,
) -> EdgeNode:
    settings = EdgeSettings(
        site_id=SITE,
        node_id=NODE,
        username=DEVICE[0],
        password=DEVICE[1],  # type: ignore[arg-type]
        streams={cid: STREAM for cid in sources},
        state_dir=str(tmp_path / "state"),
        config_refresh_s=10,
        drain_interval_s=1,
        **overrides,
    )
    return EdgeNode(
        settings,
        model=SimModel(),
        publisher=publisher or Published(make_client(db, DEVICE)),
        source_factory=lambda cam_id, url: sources[cam_id],
        fetch_config=fetch or (lambda: service.edge_config(db, SITE, at=clock.now())),
        evidence=evidence,
        now=clock.now,
        clock=clock.mono,
    )


def run(node: EdgeNode, clock: Clock, until: datetime, step_s: float = 0.2) -> None:
    while clock.t <= until:
        node.step()
        clock.t += timedelta(seconds=step_s)


def fake_jpeg(image: Any) -> bytes:
    return b"\xff\xd8fake-jpeg\xff\xd9"


# -- the node, end to end --------------------------------------------------------------------------


def test_node_intruder_one_alarm_with_edge_evidence_and_no_secret_in_logs(
    db_session: Session, cam: Camera, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    fs = sim_frames("intruder")
    src = FakeSource(fs)
    ev = EvidenceStore(tmp_path / "ev", site_id=SITE, node_id=NODE, mode="edge", encoder=fake_jpeg)
    node = make_node(db_session, tmp_path, {cam.id: src}, clock, evidence=ev)
    node.start()
    assert src.started and node.config_source == "core"
    # 16 s of frames at 5 fps, stepped faster than they arrive; a config refresh happens mid-scene
    # (refresh every 10 s) with nothing changed — it must not rebuild the camera or re-alarm.
    runtime_before = node.cameras[cam.id].runtime
    run(node, clock, fs[-1].ts)
    assert node.cameras[cam.id].runtime is runtime_before
    node.shutdown()
    assert src.stopped

    alarms = [a for a in list_events(db_session, site_id=SITE) if a.type == "zone_breach"]
    assert len(alarms) == 1
    clip = alarms[0].evidence["clip_uri"]
    assert clip.startswith(f"edge://{NODE}/{cam.id}/") and clip.endswith(".jpg")
    stills = list((tmp_path / "ev" / "kept").rglob("*.jpg"))
    assert len(stills) == 1 and stills[0].read_bytes().startswith(b"\xff\xd8")
    # Config cached for an offline boot; heartbeat file touched for the container healthcheck.
    cached = json.loads((tmp_path / "state" / "config.json").read_text())
    assert cached["site_id"] == SITE
    assert (tmp_path / "state" / "heartbeat").exists()
    health = db_session.execute(select(VisionNodeHealth)).scalars().all()
    assert health and all(h.node_id == NODE for h in health)
    assert health[0].status == "ok" and health[0].cameras_active == 1
    assert health[-1].status == "down"  # a clean stop says so, rather than going quiet
    # The camera password never reaches a log line or the core.
    assert not any("s3cr3t-pass" in json.dumps(vars(r), default=str) for r in caplog.records)
    assert "s3cr3t-pass" not in json.dumps(cached)


def test_node_boots_offline_from_cached_config_and_degrades(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    clock = Clock()
    first = make_node(db_session, tmp_path, {cam.id: FakeSource()}, clock)
    first.start()
    first.shutdown()
    clock.t += timedelta(seconds=1)  # a restart is a later instant (health ids are per-instant)

    def down() -> dict[str, Any]:
        raise urllib.error.URLError("core unreachable")

    fs = sim_frames("intruder")
    publisher = Published(make_client(db_session, DEVICE))
    offline = make_node(
        db_session, tmp_path, {cam.id: FakeSource(fs)}, clock, fetch=down, publisher=publisher
    )
    offline.start()
    assert offline.config_source == "cache" and cam.id in offline.cameras
    run(offline, clock, fs[-1].ts)
    offline.shutdown()
    # Detection and alarming ran from the cache; the node said it was degraded.
    assert [a.type for a in list_events(db_session, site_id=SITE)].count("zone_breach") == 1
    sent_health = [p for p in publisher.payloads if p["schema"] == "vision.health.v1"]
    assert sent_health[0]["status"] == "degraded"
    assert "config fetch failed" in (sent_health[0]["last_error"] or "")

    # No core and no cache: refuse to guess.
    (tmp_path / "state" / "config.json").unlink()
    blind = make_node(db_session, tmp_path, {cam.id: FakeSource()}, clock, fetch=down)
    with pytest.raises(ConfigUnavailableError):
        blind.start()


def test_dead_stream_reports_no_signal_and_core_raises_one_camera_offline(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    clock = Clock()
    src = FakeSource(connected=False)
    publisher = Published(make_client(db_session, DEVICE))
    node = make_node(db_session, tmp_path, {cam.id: src}, clock, publisher=publisher)
    node.start()
    run(node, clock, START + timedelta(seconds=400), step_s=5)
    node.flush(clock.t)

    states = db_session.execute(select(VisionCameraHealth.state)).scalars().all()
    assert states and set(states) == {"no_signal"}
    # Heartbeat-limited: a dead camera costs one health record a minute, not one per loop.
    assert len(states) <= 8
    node_health = [p for p in publisher.payloads if p["schema"] == "vision.health.v1"]
    assert node_health[-1]["status"] == "degraded" and node_health[-1]["cameras_active"] == 0
    assert node_health[-1]["last_error"] == "stream did not open"

    # Health is *fresh* (the node keeps reporting), yet the camera is blind: offline after 300 s.
    events = service.detect_camera_offline(db_session, SITE, stale_s=300, now=clock.t)
    assert len(events) == 1
    assert events[0].type == "camera_offline" and events[0].detail["reason"] == "no_signal"
    assert "no signal" in events[0].summary
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=clock.t) == []


def test_no_signal_shorter_than_the_window_is_not_offline(db_session: Session, cam: Camera) -> None:
    def health(ts: datetime, state: str) -> None:
        db_session.add(
            VisionCameraHealth(
                health_id=f"h-{ts.isoformat()}",
                site_id=SITE,
                node_id=NODE,
                camera_id=cam.id,
                ts=ts,
                received_at=ts,
                state=state,
            )
        )

    health(START, "ok")
    health(START + timedelta(seconds=200), "no_signal")
    health(START + timedelta(seconds=290), "no_signal")
    db_session.commit()
    now = START + timedelta(seconds=300)
    # Dark for 100 s only (since the last good frame at +0 … first no_signal at +200).
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=now) == []


def test_frozen_picture_is_a_condition_not_an_offline_alarm(
    db_session: Session, cam: Camera
) -> None:
    """Seen on the container smoke: a static scene reads as a frozen picture (``no_signal`` *with*
    a frame). That must not page anyone as offline — only "no frames arrive" does."""
    for i in range(8):
        ts = START + timedelta(seconds=60 * i)
        db_session.add(
            VisionCameraHealth(
                health_id=f"frozen-{i}",
                site_id=SITE,
                node_id=NODE,
                camera_id=cam.id,
                ts=ts,
                received_at=ts,
                state="no_signal",
                frozen_frames=25,
                last_frame_at=ts,
            )
        )
    db_session.commit()
    now = START + timedelta(seconds=430)
    assert service.detect_camera_offline(db_session, SITE, stale_s=300, now=now) == []


def test_config_changes_rebuild_only_what_changed(
    db_session: Session, cam: Camera, tmp_path: Path
) -> None:
    clock = Clock()
    node = make_node(
        db_session, tmp_path, {cam.id: FakeSource(), "cam-no-scene": FakeSource()}, clock
    )
    node.start()
    config = node.config
    rt = node.cameras[cam.id].runtime
    assert "cam-no-scene" not in node.cameras  # a stream with no scene is reported, not analysed

    same = node.apply_config(json.loads(json.dumps(config)))
    assert same["rebuilt"] == [] and node.cameras[cam.id].runtime is rt

    # Rules change only: a fresh rule engine, but tracks and the open rollup bucket carry over.
    rules_changed = json.loads(json.dumps(config))
    rules_changed["rules"] = rules_changed["rules"][:1]
    assert node.apply_config(rules_changed)["rebuilt"] == [cam.id]
    rt2 = node.cameras[cam.id].runtime
    assert rt2 is not rt and rt2.tracker is rt.tracker and rt2.rollup is rt.rollup

    # The scene geometry changes: new tracker, and a rollup epoch so ids never collide.
    scene_changed = json.loads(json.dumps(rules_changed))
    scene_changed["cameras"][0]["image_zones"] = scene_changed["cameras"][0]["image_zones"][:1]
    assert node.apply_config(scene_changed)["rebuilt"] == [cam.id]
    rt3 = node.cameras[cam.id].runtime
    assert rt3.tracker is not rt2.tracker and rt3.rollup.epoch == "e1"

    # Stream removed from the node's settings → camera stopped cleanly.
    node.s.streams = {}
    assert node.apply_config(scene_changed)["stopped"] == [cam.id]
    assert node.cameras == {}


def test_camera_fingerprint_ignores_generated_at_and_other_cameras() -> None:
    base = {
        "site_id": SITE,
        "generated_at": "2026-09-28T08:00:00+00:00",
        "cameras": [
            {"camera_id": "a", "image_zones": [{"zone_id": "z"}]},
            {"camera_id": "b", "image_zones": []},
        ],
        "zones": {"z": {"kind": "restricted"}, "other": {"kind": "generic"}},
        "rules": [],
    }
    later = json.loads(json.dumps(base))
    later["generated_at"] = "2026-09-28T09:00:00+00:00"
    later["cameras"][1]["image_zones"] = [{"zone_id": "other"}]
    later["zones"]["other"] = {"kind": "restricted"}
    assert camera_fingerprint(base, "a") == camera_fingerprint(later, "a")
    assert camera_fingerprint(base, "missing") is None


# -- evidence ------------------------------------------------------------------------------------


def test_evidence_modes_and_ring_bounds(tmp_path: Path) -> None:
    ts = START
    off = EvidenceStore(tmp_path / "off", site_id=SITE, node_id=NODE, mode="off")
    assert off.save(camera_id="c", event_id="e1", ts=ts, image=object()) is None

    edge = EvidenceStore(
        tmp_path / "edge", site_id=SITE, node_id=NODE, encoder=fake_jpeg, max_items=3
    )
    refs = [edge.save(camera_id="c 1", event_id=f"e/{i}", ts=ts, image=object()) for i in range(5)]
    assert refs[0] == f"edge://{NODE}/c-1/e-0.jpg"  # path-safe, deterministic
    for i, p in enumerate(sorted((tmp_path / "edge" / "kept").rglob("*.jpg"))):
        os.utime(p, (1_000 + i, 1_000 + i))
    assert edge.prune(datetime.fromtimestamp(1_010, UTC)) == 2  # oldest beyond max_items
    assert edge.prune(datetime.fromtimestamp(1_000 + 8 * 86_400, UTC)) == 3  # past max_age

    def broken(_: Any) -> bytes:
        raise ValueError("encoder exploded")

    bad = EvidenceStore(tmp_path / "bad", site_id=SITE, node_id=NODE, encoder=broken)
    assert bad.save(camera_id="c", event_id="e", ts=ts, image=object()) is None  # never raises

    with pytest.raises(ValueError):
        EvidenceStore(tmp_path / "x", site_id=SITE, node_id=NODE, mode="s3")


class FlakyUploader(S3Uploader):
    def __init__(self) -> None:
        super().__init__("http://minio:9000", "mine-evidence", "ak", "sk")
        self.down = True
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        if self.down:
            raise S3Error("connection refused")
        self.puts.append((key, data))
        return self.uri(key)


def test_s3_evidence_is_store_and_forward(tmp_path: Path) -> None:
    up = FlakyUploader()
    store = EvidenceStore(
        tmp_path, site_id=SITE, node_id=NODE, mode="s3", uploader=up, encoder=fake_jpeg
    )
    ref = store.save(camera_id="cam-1", event_id="vop-1", ts=START, image=object())
    # The reference is final and site-prefixed before the upload happens (per-site deletable).
    assert ref == f"s3://mine-evidence/{SITE}/vision/cam-1/2026/09/28/vop-1.jpg"
    assert store.upload_pending() == {"uploaded": 0, "pending": 1}  # link down: kept, not lost
    up.down = False
    assert store.upload_pending() == {"uploaded": 1, "pending": 0}
    assert up.puts == [(f"{SITE}/vision/cam-1/2026/09/28/vop-1.jpg", fake_jpeg(None))]
    assert store.upload_pending() == {"uploaded": 0, "pending": 0}  # sent exactly once


def test_clip_ref_scheme_is_allow_listed() -> None:
    window = (START, START + timedelta(seconds=1))
    for ok in ("edge://n/c/e.jpg", "s3://b/k.jpg", "nvr://ch3/x.mp4", None):
        assert Evidence(observation_window=window, clip_ref=ok).clip_ref == ok
    bad_refs = ("http://evil/x.jpg", "file:///etc/passwd", "edge://has space", "data:image/jpeg")
    for bad in bad_refs:
        with pytest.raises(ValidationError):
            Evidence(observation_window=window, clip_ref=bad)
    # The published JSON Schema (the source of truth) agrees with the model.
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "vision.operational_event.v1.json").read_text()
    )
    pattern = schema["$defs"]["evidence"]["properties"]["clip_ref"]["pattern"]
    assert all(re.search(pattern, ok) for ok in ("edge://n/c/e.jpg", "s3://b/k", "nvr://c/1"))
    assert not any(re.search(pattern, bad) for bad in bad_refs)


# -- S3 signing -----------------------------------------------------------------------------------


def test_sigv4_matches_the_aws_worked_example() -> None:
    """AWS S3 docs, "Example: GET Object" (Signature Version 4)."""
    h = sign_v4(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        query=None,
        headers={"Range": "bytes=0-9"},
        payload_sha256=EMPTY_SHA256,
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        when=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert h["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


class _VerifyingS3(BaseHTTPRequestHandler):
    """Accepts a PUT only if its SigV4 signature re-derives with the server's copy of the key."""

    secret = "minio-secret"
    objects: dict[str, bytes] = {}

    def do_PUT(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        auth = self.headers["Authorization"]
        signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
        when = datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        expect = sign_v4(
            method="PUT",
            host=self.headers["Host"],
            path=urllib.parse.unquote(self.path),
            query=None,
            headers={k: self.headers[k] for k in signed if k not in ("host", "x-amz-date")}
            | {"x-amz-content-sha256": self.headers["x-amz-content-sha256"]},
            payload_sha256=hashlib.sha256(body).hexdigest(),
            access_key="minio-user",
            secret_key=self.secret,
            region="us-east-1",
            when=when,
        )["authorization"]
        ok = (
            auth == expect
            and self.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
        )
        if ok:
            self.objects[self.path] = body
        self.send_response(200 if ok else 403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        pass


def test_s3_uploader_round_trip_against_a_verifying_server() -> None:
    server = HTTPServer(("127.0.0.1", 0), _VerifyingS3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        good = S3Uploader(endpoint, "mine-evidence", "minio-user", "minio-secret")
        uri = good.put(f"{SITE}/vision/cam-1/a b.jpg", b"jpeg-bytes", content_type="image/jpeg")
        assert uri == f"s3://mine-evidence/{SITE}/vision/cam-1/a b.jpg"
        assert _VerifyingS3.objects[f"/mine-evidence/{SITE}/vision/cam-1/a%20b.jpg"] == (
            b"jpeg-bytes"
        )
        wrong = S3Uploader(endpoint, "mine-evidence", "minio-user", "not-the-secret")
        with pytest.raises(S3Error, match="403"):
            wrong.put("k.jpg", b"x")
    finally:
        server.shutdown()
    with pytest.raises(S3Error):
        S3Uploader(endpoint, "b", "a", "s", timeout_s=0.5).put("k", b"x")  # server gone
    with pytest.raises(ValueError):
        S3Uploader("minio:9000", "b", "a", "s")


# -- live source (fake capture; the real decoder is in test_vision_yolox.py) -------------------


class FakeCapture:
    def __init__(self, frames_: int, *, opened: bool = True) -> None:
        self.left = frames_
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def read(self) -> tuple[bool, Any]:
        if self.left <= 0:
            return False, None
        self.left -= 1
        return True, object()

    def release(self) -> None:
        self.released = True


def test_redact_url_hides_only_the_password() -> None:
    assert redact_url(STREAM) == (
        "rtsp://admin:***@10.20.0.5:554/cam/realmonitor?channel=1&subtype=1"
    )
    assert redact_url("rtsp://10.0.0.1/stream") == "rtsp://10.0.0.1/stream"
    assert "s3cr3t" not in redact_url("rtsp://u:s3cr3t@[fe80::1]/x")


def test_backoff_grows_caps_and_resets() -> None:
    b = Backoff(initial_s=1, maximum_s=8, jitter=0)
    assert [b.next() for _ in range(6)] == [1, 2, 4, 8, 8, 8]
    b.reset()
    assert b.next() == 1
    jittery = Backoff(initial_s=10, jitter=0.2)
    assert all(8 <= jittery.next() <= 12 for _ in range(1)) and jittery.next() <= 24


def test_rtsp_source_samples_drops_old_and_reconnects(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    t = [0.0]
    caps = [FakeCapture(0, opened=False), FakeCapture(30), FakeCapture(10)]
    opened: list[str] = []

    def opener(url: str) -> FakeCapture:
        opened.append(url)
        return caps.pop(0)

    src = RtspSource(
        STREAM,
        camera_id="cam-1",
        sample_fps=5,
        opener=opener,
        backoff=Backoff(initial_s=2, jitter=0),
        clock=lambda: t[0],
        now=lambda: START + timedelta(seconds=t[0]),
        sleep=lambda s: None,
    )
    src.pump()  # first open fails → retry scheduled 2 s out, not a hot loop
    assert not src.connected and src.last_error == "stream did not open"
    src.pump()
    assert len(opened) == 1  # still backing off
    t[0] = 2.0
    # Camera at 25 fps (0.04 s/frame); analyser at 5 fps takes a frame only every 0.2 s.
    for _ in range(30):
        src.pump()
        t[0] += 0.04
    assert src.connected and src.opens == 1
    assert src.frames == 7  # 1.2 s at 5 fps: a sample at 2.0 s, then one per 0.2 s slot
    assert src.dropped == 6  # nobody consumed: each new sample replaced an unanalysed one
    f = src.latest()
    assert f is not None and abs((f.ts - START).total_seconds() - 3.16) < 1e-3  # the newest
    assert f.frame_id == f"cam-1-{f.ts.strftime('%Y%m%dT%H%M%S%fZ')}" and src.latest() is None

    for _ in range(3):  # the stream stalls: 3 failed reads → drop and reconnect after backoff
        src.pump()
    assert not src.connected and src.last_error == "stream stopped delivering frames"
    t[0] += 2.0
    src.pump()
    src.pump()
    assert src.connected and src.reconnects == 1
    assert src.status()["reconnects"] == 1
    src.stop()
    urls = {getattr(r, "url", None) for r in caplog.records} - {None}
    assert urls == {redact_url(STREAM)}  # every line names the camera, none carries the password
    assert not any("s3cr3t-pass" in json.dumps(vars(r), default=str) for r in caplog.records)


@pytest.mark.parametrize("stream_fps", [5.0, 12.5, 25.0])
def test_rtsp_source_holds_the_sample_rate_under_arrival_jitter(stream_fps: float) -> None:
    """Regression: scheduling from arrival time skipped any frame a few ms early, halving the
    analysed rate when the camera sends exactly ``sample_fps`` (seen on the container smoke)."""
    import random

    rng = random.Random(1)
    t = [0.0]
    src = RtspSource(
        "rtsp://cam",
        camera_id="c",
        sample_fps=5,
        opener=lambda url: FakeCapture(10_000),
        clock=lambda: t[0],
        sleep=lambda s: None,
    )
    for i in range(int(stream_fps * 60)):
        t[0] = i / stream_fps + rng.uniform(-0.01, 0.01)
        src.pump()
        src.latest()
    assert 295 <= src.frames <= 305  # 5 fps over 60 s, whatever the camera's own rate
    assert src.dropped == 0  # consumed promptly: nothing shed


def test_rtsp_source_thread_hands_over_frames_and_stops() -> None:
    src = RtspSource(
        "rtsp://cam", camera_id="c", sample_fps=1000, opener=lambda url: FakeCapture(10_000)
    ).start()
    try:
        f = src.latest(timeout_s=2.0)
        assert f is not None and f.camera_state is None
    finally:
        src.stop()
    assert not src.connected


# -- broker identity -----------------------------------------------------------------------------


def test_vision_edge_device_acl_and_never_authorises_positions(db_session: Session) -> None:
    devices.register_device(db_session, device_id="trk-1", site_id=SITE, asset_id="HT-102")
    devices.register_device(
        db_session, device_id="edge-01", site_id=SITE, asset_id="edge-01", source=VISION_EDGE_SOURCE
    )
    db_session.commit()
    acl = render_mosquitto_acl(db_session, ingest_user="mm-ingestor")
    assert f"user edge-01\ntopic write mm/{SITE}/vision/#" in acl
    assert f"user trk-1\ntopic write mm/{SITE}/HT-102/position" in acl
    assert f"mm/{SITE}/edge-01/position" not in acl
    assert devices.authorized_asset(db_session, SITE, "HT-102") is True
    assert devices.authorized_asset(db_session, SITE, "edge-01") is False


def test_vision_topic_names_camera_or_node() -> None:
    assert (
        vision_topic("mm", {"schema": "vision.zone_rollup.v1", "site_id": SITE, "camera_id": "c1"})
        == f"mm/{SITE}/vision/c1/zone_rollup"
    )
    assert (
        vision_topic("mm", {"schema": "vision.health.v1", "site_id": SITE, "node_id": "edge-01"})
        == f"mm/{SITE}/vision/edge-01/health"
    )
    with pytest.raises(RejectedRecordError):
        vision_topic("mm", {"schema": "asset.position.v1", "site_id": SITE})


# -- container healthcheck -----------------------------------------------------------------------


def test_healthcheck_reads_heartbeat_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_EDGE_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="no heartbeat"):
        cli_main(["healthcheck"])
    beat = tmp_path / "heartbeat"
    beat.write_text("now")
    cli_main(["healthcheck"])
    old = time.time() - 600
    os.utime(beat, (old, old))
    with pytest.raises(SystemExit, match="unhealthy"):
        cli_main(["healthcheck", "--max-age", "120"])


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_EDGE_SITE_ID", SITE)
    monkeypatch.setenv("MM_EDGE_USERNAME", "edge-01")
    monkeypatch.setenv("MM_EDGE_PASSWORD", "pw")
    monkeypatch.setenv("MM_EDGE_STREAMS", json.dumps({"cam-1": STREAM}))
    s = EdgeSettings()  # type: ignore[call-arg]
    assert s.streams == {"cam-1": STREAM} and s.transport == "http" and s.evidence == "edge"
    assert "pw" not in repr(s)  # secrets are SecretStr
    monkeypatch.setenv("MM_EDGE_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValidationError):
        EdgeSettings()  # type: ignore[call-arg]


def test_run_node_misconfiguration_never_echoes_stream_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MM_EDGE_STREAMS", json.dumps({"cam-1": STREAM}))  # password missing
    for var in ("MM_EDGE_SITE_ID", "MM_EDGE_USERNAME", "MM_EDGE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli_main(["run-node"])
    msg = str(exc.value)
    assert "MM_EDGE_PASSWORD: Field required" in msg and "MM_EDGE_SITE_ID" in msg
    assert "s3cr3t-pass" not in msg + capsys.readouterr().err
