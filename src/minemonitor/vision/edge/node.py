"""The vision edge node service: every camera at a site, one process, one model, one outbox.

``python -m minemonitor.vision.edge.cli run-node`` (the ``vision-edge`` compose service) runs this.
It is the Stage C pilot shape from ``docs/VISION_DEPLOYMENT.md``:

```
camera streams ─► RtspSource (grabber thread each: reconnect, keep newest frame)
               ─► EdgeRuntime per camera (detect → track → zones → rules → rollups → health)
               ─► Outbox (SQLite, crash-safe) ─► HTTP or MQTT publisher ─► core
               ─► EvidenceStore (a still per alarm; edge ring, optional S3 upload)
```

Operating behaviour, all designed for a remote site (``CLAUDE.md`` §3):

- **Config is cached.** The node pulls ``GET /vision/config`` at start and every
  ``config_refresh_s``, and writes it to ``state_dir/config.json`` (atomically). If the core is
  unreachable at boot — a power cut that took the core's box down too — it runs from the cache.
- **A config refresh changes only what changed.** A camera is rebuilt only when *its* scene, zone
  policy or rules changed; an unchanged camera keeps its tracker and rule state, so a periodic
  refresh can never re-raise an alarm already raised. When only rules change, tracks and the open
  rollup bucket carry over.
- **A dead stream is reported, not silent.** No frame for ``no_signal_after_s`` → the camera
  reports ``no_signal`` (the core turns sustained ``no_signal`` into a ``camera_offline`` alarm).
- **Node health** (one ``vision.health.v1`` per heartbeat): cameras connected, analysed fps, frames
  shed under load, outbox depth; ``degraded`` while any camera is down.
- **Shutdown is clean on SIGTERM** (``docker compose stop``): open rollups flushed, a final drain.
  An *unclean* stop loses nothing either — everything was in the outbox first.
- ``state_dir/heartbeat`` is touched every loop; the container healthcheck reads its age.

Stream URLs carry camera passwords: they come from the environment (``MM_EDGE_STREAMS``), never
from the core, and are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import secrets
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from minemonitor import net
from minemonitor.contracts.vision import VisionHealthV1
from minemonitor.vision.adapters.base import VisionModel
from minemonitor.vision.edge.evidence import EvidenceStore, write_atomic
from minemonitor.vision.edge.outbox import Outbox
from minemonitor.vision.edge.publish import Publisher, drain
from minemonitor.vision.edge.runtime import EdgeRuntime, FrameIn
from minemonitor.vision.rollup import RollupAccumulator

log = logging.getLogger("minemonitor.vision.edge.node")


class EdgeSettings(BaseSettings):
    """Edge node configuration, from ``MM_EDGE_*`` environment variables (see ``.env.example``)."""

    # Empty variables count as unset, so compose can pass ``${VAR:-}`` through safely.
    model_config = SettingsConfigDict(env_prefix="MM_EDGE_", extra="ignore", env_ignore_empty=True)

    site_id: str
    node_id: str = "edge-01"
    core_url: str = "http://api:8000"
    # The node's *device* account on the core API (config pull + HTTP ingest).
    username: str
    password: SecretStr
    # {"<camera_id>": "rtsp://user:pass@host/stream"} — secrets; never logged, never sent.
    streams: dict[str, str] = Field(default_factory=dict)
    model: str = "yolox-nano"
    models_dir: str = "var/models"
    sample_fps: float = Field(default=5.0, gt=0, le=30)
    state_dir: str = "var/edge"
    transport: Literal["http", "mqtt"] = "http"
    mqtt_host: str = "mqtt"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")
    mqtt_prefix: str = "mm"
    config_refresh_s: float = Field(default=300.0, ge=10)
    drain_interval_s: float = Field(default=5.0, gt=0)
    heartbeat_s: int = Field(default=60, ge=5)
    no_signal_after_s: float = Field(default=15.0, gt=0)
    # Frozen-picture detection, tuned on commissioning (a compressed static scene can look frozen).
    frozen_diff: float = Field(default=0.5, ge=0)
    frozen_frames: int = Field(default=25, ge=2)
    observation_keep_hours: float = Field(default=72.0, gt=0)
    evidence: Literal["off", "edge", "s3"] = "edge"
    evidence_max_items: int = Field(default=500, ge=1)
    evidence_max_age_days: float = Field(default=7.0, gt=0)
    s3_endpoint: str = "http://objectstore:8333"
    s3_bucket: str = "mine-evidence"
    s3_access_key: str = ""
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"

    @field_validator("core_url")
    @classmethod
    def _core_url_https_off_box(cls, v: str) -> str:
        """The node sends its device password (HTTP Basic) on every request: plain ``http`` is
        accepted only on the same box — loopback or a single-label compose service name such as
        ``api``. A core on another host must be ``https``."""
        parts = urllib.parse.urlsplit(v)
        host = (parts.hostname or "").lower()
        if parts.scheme == "https" and host:
            return v
        if parts.scheme == "http" and _same_box_host(host):
            return v
        raise ValueError(
            "must be https:// for a core on another host (the device password is sent with "
            "every request); plain http is allowed only for localhost or a compose service name"
        )


def _same_box_host(host: str) -> bool:
    """Loopback (a literal loopback IP, not a name that merely starts with ``127.``) or a
    single-label name, which in this deployment is a compose service on the same Docker host."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host != "" and "." not in host and ":" not in host


class FrameSource(Protocol):
    """What the node needs from a camera source (:class:`RtspSource`, or a fake in tests)."""

    connected: bool
    dropped: int
    last_frame_at: datetime | None
    last_error: str | None

    def start(self) -> Any: ...

    def stop(self) -> None: ...

    def latest(self, timeout_s: float = 0.0) -> FrameIn | None: ...


class ConfigUnavailableError(Exception):
    """Neither the core nor the local cache could provide the edge config."""


def fetch_edge_config(core_url: str, site_id: str, username: str, password: str) -> dict[str, Any]:
    """``GET /api/v1/sites/<site>/vision/config`` as the node's device account."""
    req = urllib.request.Request(
        f"{core_url.rstrip('/')}/api/v1/sites/{site_id}/vision/config",
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        },
    )
    with net.urlopen(req, timeout=15) as resp:
        doc = json.loads(resp.read())
    if not isinstance(doc, dict) or doc.get("site_id") != site_id or "cameras" not in doc:
        raise ValueError("edge config response is not a vision config for this site")
    return doc


def camera_fingerprint(config: dict[str, Any], camera_id: str) -> str | None:
    """Hash of everything that shapes one camera's runtime (``None``: camera has no scene)."""
    cam = next((c for c in config.get("cameras", []) if c.get("camera_id") == camera_id), None)
    if cam is None:
        return None
    zone_ids = sorted({z["zone_id"] for z in cam.get("image_zones", [])})
    relevant = {
        "camera": cam,
        "zones": {z: config.get("zones", {}).get(z) for z in zone_ids},
        "rules": config.get("rules", []),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


def _scene_key(config: dict[str, Any], camera_id: str) -> str:
    cam: dict[str, Any] = next(
        (c for c in config.get("cameras", []) if c.get("camera_id") == camera_id), {}
    )
    return json.dumps(
        [cam.get("image_zones", []), cam.get("homography")], sort_keys=True, default=str
    )


@dataclass
class _Camera:
    camera_id: str
    source: FrameSource
    runtime: EdgeRuntime
    fingerprint: str
    scene_key: str
    started_at: datetime
    epoch: int = 0


class EdgeNode:
    """Run every configured camera of one node until stopped."""

    def __init__(
        self,
        settings: EdgeSettings,
        *,
        model: VisionModel,
        publisher: Publisher,
        source_factory: Callable[[str, str], FrameSource],
        fetch_config: Callable[[], dict[str, Any]] | None = None,
        evidence: EvidenceStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.s = settings
        self.model = model
        self.publisher = publisher
        self.source_factory = source_factory
        self._fetch = fetch_config or (
            lambda: fetch_edge_config(
                settings.core_url,
                settings.site_id,
                settings.username,
                settings.password.get_secret_value(),
            )
        )
        self._now = now
        self._clock = clock
        self.state_dir = Path(settings.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.state_dir / "config.json"
        self.heartbeat_path = self.state_dir / "heartbeat"
        self.outbox = Outbox(self.state_dir / "outbox.sqlite")
        self.evidence = evidence
        self.cameras: dict[str, _Camera] = {}
        self.config: dict[str, Any] = {}
        self.config_source: Literal["core", "cache"] | None = None
        self._stopping = False
        self._last_refresh = 0.0
        self._last_drain = 0.0
        self._last_health: datetime | None = None
        self._last_touch = 0.0
        self._frames_since_health = 0
        self._health_window_start = self._clock()
        self.last_error: str | None = None
        # Unique per process: a restart inside a rollup bucket must not reuse the id of the bucket
        # flushed at shutdown, or the outbox and the core would drop the second half as a replay.
        self._boot = secrets.token_hex(3)
        info = model.info()
        self._model_id = f"{info.id}@{info.version}"

    # -- config ------------------------------------------------------------------------------

    def load_config(self) -> dict[str, Any]:
        """Fetch from the core and cache; on failure fall back to the cached copy."""
        try:
            doc = self._fetch()
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            self.last_error = f"config fetch failed: {type(exc).__name__}"
            if self.cache_path.exists():
                log.warning(
                    "core unreachable; running from cached vision config",
                    extra={"site_id": self.s.site_id, "error": str(exc)},
                )
                self.config_source = "cache"
                return dict(json.loads(self.cache_path.read_text()))
            raise ConfigUnavailableError(
                "core unreachable and no cached config — the node needs one successful "
                "config pull before it can run offline"
            ) from exc
        write_atomic(self.cache_path, json.dumps(doc, sort_keys=True).encode())
        self.config_source = "core"
        return doc

    def apply_config(self, config: dict[str, Any]) -> dict[str, list[str]]:
        """Start, rebuild or stop cameras to match ``config``. Returns what changed."""
        now = self._now()
        changes: dict[str, list[str]] = {"started": [], "rebuilt": [], "stopped": [], "idle": []}
        wanted = set(self.s.streams)
        for cam_id in sorted(set(self.cameras) - wanted):
            self._stop_camera(cam_id, now)
            changes["stopped"].append(cam_id)
        for cam_id in sorted(wanted):
            fp = camera_fingerprint(config, cam_id)
            current = self.cameras.get(cam_id)
            if fp is None:
                # A stream with no scene configured yet: nothing to analyse against. Say so.
                if current is not None:
                    self._stop_camera(cam_id, now)
                    changes["stopped"].append(cam_id)
                log.warning(
                    "camera has a stream but no scene config — set one with PUT .../scene",
                    extra={"site_id": self.s.site_id, "camera_id": cam_id},
                )
                changes["idle"].append(cam_id)
                continue
            if current is None:
                self.cameras[cam_id] = self._start_camera(cam_id, config, fp, now)
                changes["started"].append(cam_id)
            elif current.fingerprint != fp:
                self._rebuild_camera(current, config, fp, now)
                changes["rebuilt"].append(cam_id)
        self.config = config
        if any(changes[k] for k in ("started", "rebuilt", "stopped")):
            log.info("vision config applied", extra={"site_id": self.s.site_id, **changes})
        return changes

    def _runtime(
        self,
        cam_id: str,
        config: dict[str, Any],
        *,
        carry: EdgeRuntime | None = None,
        epoch: int = 0,
    ) -> EdgeRuntime:
        rollup = carry.rollup if carry is not None else None
        if rollup is None:
            cam = next(c for c in config["cameras"] if c["camera_id"] == cam_id)
            rollup = RollupAccumulator(
                site_id=self.s.site_id,
                node_id=self.s.node_id,
                camera_id=cam_id,
                zone_ids=[z["zone_id"] for z in cam.get("image_zones", [])],
                epoch=f"{self._boot}e{epoch}",
            )
        return EdgeRuntime(
            site_id=self.s.site_id,
            node_id=self.s.node_id,
            camera_id=cam_id,
            config=config,
            model=self.model,
            outbox=self.outbox,
            heartbeat_s=self.s.heartbeat_s,
            evidence=self.evidence,
            node_health=False,
            tracker=carry.tracker if carry is not None else None,
            rollup=rollup,
        )

    def _start_camera(self, cam_id: str, config: dict[str, Any], fp: str, now: datetime) -> _Camera:
        source = self.source_factory(cam_id, self.s.streams[cam_id])
        source.start()
        return _Camera(
            camera_id=cam_id,
            source=source,
            runtime=self._runtime(cam_id, config),
            fingerprint=fp,
            scene_key=_scene_key(config, cam_id),
            started_at=now,
        )

    def _rebuild_camera(self, cam: _Camera, config: dict[str, Any], fp: str, now: datetime) -> None:
        scene_key = _scene_key(config, cam.camera_id)
        if scene_key == cam.scene_key:
            # Rules or zone policy changed; the image geometry did not. Keep tracks and the open
            # rollup bucket so nothing is double-counted.
            cam.runtime = self._runtime(cam.camera_id, config, carry=cam.runtime)
        else:
            # The scene itself changed: close the old geometry's bucket, start a fresh one whose
            # ids cannot collide with the bucket just closed.
            cam.runtime.close(now)
            cam.epoch += 1
            cam.runtime = self._runtime(cam.camera_id, config, epoch=cam.epoch)
            cam.scene_key = scene_key
        cam.fingerprint = fp

    def _stop_camera(self, cam_id: str, now: datetime) -> None:
        cam = self.cameras.pop(cam_id)
        cam.source.stop()
        cam.runtime.close(now)

    # -- loop --------------------------------------------------------------------------------

    def step(self) -> int:
        """One pass over every camera. Returns frames analysed (0 ⇒ the caller may sleep)."""
        now = self._now()
        t = self._clock()
        if t - self._last_refresh >= self.s.config_refresh_s:
            self._last_refresh = t
            self._refresh()
        processed = 0
        for cam in self.cameras.values():
            frame = cam.source.latest()
            if frame is not None:
                cam.runtime.process(frame)
                cam.runtime.note_dropped(cam.source.dropped)
                processed += 1
                continue
            last = cam.source.last_frame_at or cam.started_at
            if now - last >= timedelta(seconds=self.s.no_signal_after_s):
                cam.runtime.idle(now, "no_signal")
        self._frames_since_health += processed
        self._node_health(now)
        if t - self._last_drain >= self.s.drain_interval_s:
            self._last_drain = t
            self.flush(now)
        if t - self._last_touch >= 5.0:
            self._last_touch = t
            write_atomic(self.heartbeat_path, now.isoformat().encode())
        return processed

    def _refresh(self) -> None:
        try:
            config = self.load_config()
        except ConfigUnavailableError as exc:
            log.error("no vision config available", extra={"error": str(exc)})
            return
        self.apply_config(config)

    def flush(self, now: datetime) -> dict[str, int]:
        """Deliver the outbox and evidence; prune local state to its bounds."""
        result = drain(self.outbox, self.publisher)
        if self.evidence is not None:
            result.update(self.evidence.upload_pending())
            self.evidence.prune(now)
        self.outbox.prune_sent(now - timedelta(hours=1))
        self.outbox.prune_observations(now - timedelta(hours=self.s.observation_keep_hours))
        return result

    def _node_health(
        self, now: datetime, *, force: bool = False, status: str | None = None
    ) -> None:
        if (
            not force
            and self._last_health is not None
            and now - self._last_health < timedelta(seconds=self.s.heartbeat_s)
        ):
            return
        elapsed = max(self._clock() - self._health_window_start, 1e-6)
        connected = sum(1 for c in self.cameras.values() if c.source.connected)
        down = [c.camera_id for c in self.cameras.values() if not c.source.connected]
        errors = [c.source.last_error for c in self.cameras.values() if c.source.last_error]
        last_error = self.last_error or (errors[0] if down and errors else None)
        self.outbox.put(
            VisionHealthV1.model_validate(
                {
                    "schema": "vision.health.v1",
                    "health_id": f"vh-{self.s.node_id}-{now.strftime('%Y%m%dT%H%M%S%fZ')}",
                    "site_id": self.s.site_id,
                    "node_id": self.s.node_id,
                    "ts": now,
                    "status": status
                    or ("degraded" if down or self.config_source == "cache" else "ok"),
                    "cameras_active": connected,
                    "fps_mean": round(self._frames_since_health / elapsed, 3),
                    "queue_depth": self.outbox.counts()["pending"],
                    "dropped_frames": sum(c.source.dropped for c in self.cameras.values()),
                    "model_ids": [self._model_id],
                    "last_error": last_error[:500] if last_error else None,
                    "advisory": True,
                }
            )
        )
        self._last_health = now
        self._frames_since_health = 0
        self._health_window_start = self._clock()
        self.last_error = None

    def start(self) -> None:
        self.apply_config(self.load_config())
        self._last_refresh = self._clock()

    def stop(self) -> None:
        self._stopping = True

    def shutdown(self) -> dict[str, int]:
        """Stop sources, flush open rollups, say ``down``, deliver what we can."""
        now = self._now()
        for cam_id in list(self.cameras):
            self._stop_camera(cam_id, now)
        self._node_health(now, force=True, status="down")
        result = self.flush(now)
        self.outbox.close()
        log.info("edge node stopped", extra={"site_id": self.s.site_id, **result})
        return result

    def run(self, *, idle_sleep_s: float = 0.02) -> None:
        """Run until SIGTERM/SIGINT. Retries config at boot until one is available."""
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        delay = 5.0
        while not self._stopping:
            try:
                self.start()
                break
            except ConfigUnavailableError as exc:
                log.error("waiting for vision config", extra={"error": str(exc), "retry_s": delay})
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        log.info(
            "edge node running",
            extra={
                "site_id": self.s.site_id,
                "node_id": self.s.node_id,
                "cameras": sorted(self.cameras),
                "model": self._model_id,
                "transport": self.s.transport,
                "config_source": self.config_source,
            },
        )
        try:
            while not self._stopping:
                if self.step() == 0:
                    time.sleep(idle_sleep_s)
        finally:
            self.shutdown()


def build_node(settings: EdgeSettings) -> EdgeNode:  # pragma: no cover - wiring, needs the extra
    """Wire the production node: YOLOX on ONNX Runtime, RTSP sources, chosen transport."""
    from minemonitor.vision.edge.cli import load_model
    from minemonitor.vision.edge.evidence import jpeg_encoder
    from minemonitor.vision.edge.publish import HttpPublisher, MqttPublisher
    from minemonitor.vision.edge.quality import QualityMonitor, QualityThresholds
    from minemonitor.vision.edge.rtsp import RtspSource
    from minemonitor.vision.edge.s3 import S3Uploader

    publisher: Publisher
    if settings.transport == "mqtt":
        publisher = MqttPublisher(
            settings.mqtt_host,
            settings.mqtt_port,
            settings.mqtt_username,
            settings.mqtt_password.get_secret_value(),
            client_id=f"mm-edge-{settings.node_id}",
            prefix=settings.mqtt_prefix,
        )
    else:
        publisher = HttpPublisher(
            settings.core_url, settings.username, settings.password.get_secret_value()
        )
    evidence = None
    if settings.evidence != "off":
        uploader = None
        if settings.evidence == "s3":
            uploader = S3Uploader(
                settings.s3_endpoint,
                settings.s3_bucket,
                settings.s3_access_key,
                settings.s3_secret_key.get_secret_value(),
                region=settings.s3_region,
            )
        evidence = EvidenceStore(
            Path(settings.state_dir) / "evidence",
            site_id=settings.site_id,
            node_id=settings.node_id,
            mode=settings.evidence,
            uploader=uploader,
            encoder=jpeg_encoder(),
            max_items=settings.evidence_max_items,
            max_age=timedelta(days=settings.evidence_max_age_days),
        )
    return EdgeNode(
        settings,
        model=load_model(settings.model, settings.models_dir),
        publisher=publisher,
        source_factory=lambda cam_id, url: RtspSource(
            url,
            camera_id=cam_id,
            sample_fps=settings.sample_fps,
            quality=QualityMonitor(
                QualityThresholds(
                    frozen_diff=settings.frozen_diff, frozen_frames=settings.frozen_frames
                )
            ),
        ),
        evidence=evidence,
    )
