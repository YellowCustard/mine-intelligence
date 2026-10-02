"""Live camera source: RTSP with reconnect/backoff and *drop-old-not-recent* frame handling.

A remote mine site's camera link drops, cameras reboot, and the node may fall behind under load
(``CLAUDE.md`` §3). This source is built for that:

- **A grabber thread drains the stream continuously** and keeps only the newest sampled frame.
  An RTSP decoder that is read slower than the camera sends builds a backlog, and analysing a
  frame from 30 s ago is worse than skipping it. When the node cannot keep up, the oldest
  unanalysed frame is replaced and counted in ``dropped`` (reported in node health). Recent frames
  are never sacrificed for old ones.
- **Time-based sampling** at ``sample_fps`` (default 5), the same as :class:`VideoFileSource`, so a
  25 fps main stream and a 12 fps substream cost the same CPU.
- **Reconnect with capped exponential backoff + jitter** (1 s → 60 s). A dead camera costs a
  connection attempt a minute, not a hot loop. Several reads failing in a row count as a drop.
- **Frames are stamped with wall-clock UTC at grab time.** RTSP timestamps from consumer cameras
  are unreliable; the node's clock is NTP-disciplined.
- **Credentials never reach a log.** Stream URLs carry the camera password; every log line uses
  :func:`redact_url`.

Works with anything OpenCV/FFmpeg opens (``rtsp://``, ``http://`` MJPEG, a file path), which is how
the tests exercise reconnects against a real decoder without a camera.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from minemonitor.vision.edge.runtime import FrameIn

if TYPE_CHECKING:  # NumPy-backed; only needed when quality checks are switched on
    from minemonitor.vision.edge.quality import QualityMonitor

log = logging.getLogger("minemonitor.vision.edge.rtsp")


class Capture(Protocol):
    """The slice of ``cv2.VideoCapture`` the source uses (a fake in tests)."""

    def isOpened(self) -> bool: ...  # noqa: N802 — OpenCV's spelling

    def read(self) -> tuple[bool, Any]: ...

    def release(self) -> None: ...


def redact_url(url: str) -> str:
    """``rtsp://admin:secret@10.0.0.5/…`` → ``rtsp://admin:***@10.0.0.5/…`` for logs."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username or ''}:***@{host}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def opencv_opener(url: str) -> Capture:
    """Open a stream with FFmpeg over **TCP** (survives lossy links) and a socket timeout."""
    import cv2

    # Must be set before the capture is created. TCP interleaving avoids the smeared/grey frames
    # UDP gives on a lossy link; the timeout (µs) stops a dead camera hanging the grabber.
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000|timeout;5000000"
    )
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class Backoff:
    """Capped exponential backoff with ±``jitter`` so a site's cameras don't retry in lockstep."""

    def __init__(
        self,
        initial_s: float = 1.0,
        maximum_s: float = 60.0,
        factor: float = 2.0,
        jitter: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        self.initial_s = initial_s
        self.maximum_s = maximum_s
        self.factor = factor
        self.jitter = jitter
        self._rng = rng or random.Random()  # noqa: S311 — scheduling jitter, not security
        self._current = initial_s

    def next(self) -> float:
        base = self._current
        self._current = min(self._current * self.factor, self.maximum_s)
        return max(0.0, base * (1 + self._rng.uniform(-self.jitter, self.jitter)))

    def reset(self) -> None:
        self._current = self.initial_s


class RtspSource:
    """One live camera. ``start()`` runs the grabber thread; ``latest()`` hands over a frame."""

    def __init__(
        self,
        url: str,
        *,
        camera_id: str,
        sample_fps: float = 5.0,
        quality: QualityMonitor | None = None,
        opener: Callable[[str], Capture | None] = opencv_opener,
        backoff: Backoff | None = None,
        max_read_failures: int = 3,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self.url = url
        self.camera_id = camera_id
        self.step_s = 1.0 / sample_fps
        self.quality = quality
        self._opener = opener
        self.backoff = backoff or Backoff()
        self.max_read_failures = max_read_failures
        self._clock = clock
        self._now = now
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._cond = threading.Condition()
        self._cap: Capture | None = None
        self._slot: tuple[datetime, Any] | None = None
        self._next_attempt = 0.0
        self._next_offer = 0.0
        self._fails = 0
        self._thread: threading.Thread | None = None
        self.connected = False
        self.opens = 0
        self.reconnects = 0
        self.dropped = 0
        self.frames = 0
        self.last_frame_at: datetime | None = None
        self.last_error: str | None = None

    # -- grabber side ------------------------------------------------------------------------

    def pump(self) -> None:
        """One grabber step: (re)connect when due, else read one frame and maybe offer it."""
        if self._cap is None:
            t = self._clock()
            if t < self._next_attempt:
                self._sleep(min(self._next_attempt - t, 0.5))
                return
            cap = self._opener(self.url)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                self._schedule_retry("stream did not open")
                return
            self._cap = cap
            self.opens += 1
            if self.opens > 1:
                self.reconnects += 1
            self._fails = 0
            log.info(
                "camera stream opened",
                extra={"camera_id": self.camera_id, "url": redact_url(self.url)},
            )
        ok, image = self._cap.read()
        if not ok or image is None:
            self._fails += 1
            if self._fails >= self.max_read_failures:
                self._disconnect("stream stopped delivering frames")
            return
        self._fails = 0
        if not self.connected:
            self.connected = True
            self.backoff.reset()
        t = self._clock()
        # Fixed cadence with a quarter-step tolerance: frame arrival jitters, so a frame a few
        # ms "early" still counts. Scheduling from the *previous slot* (not from ``t``) keeps
        # the mean rate at ``sample_fps`` even when the stream runs at exactly that rate.
        if t < self._next_offer - self.step_s / 4:
            return  # between samples: decoded to keep the stream drained, not analysed
        self._next_offer += self.step_s
        if self._next_offer < t:
            self._next_offer = t + self.step_s  # after a gap (or the first frame): resync
        ts = self._now()
        with self._cond:
            if self._slot is not None:
                self.dropped += 1  # the analyser fell behind: drop the old frame, keep the new
            self._slot = (ts, image)
            self.last_frame_at = ts
            self.frames += 1
            self._cond.notify_all()

    def _schedule_retry(self, reason: str) -> None:
        delay = self.backoff.next()
        self._next_attempt = self._clock() + delay
        self.last_error = reason
        log.warning(
            "camera stream unavailable",
            extra={
                "camera_id": self.camera_id,
                "url": redact_url(self.url),
                "reason": reason,
                "retry_s": round(delay, 1),
            },
        )

    def _disconnect(self, reason: str) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001 — a broken capture must not stop reconnecting
                log.debug("capture release failed", extra={"camera_id": self.camera_id})
            self._cap = None
        self.connected = False
        self._schedule_retry(reason)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.pump()
            except Exception as exc:  # noqa: BLE001 — the grabber survives anything and retries
                log.exception("camera grabber error", extra={"camera_id": self.camera_id})
                self._disconnect(f"error: {type(exc).__name__}")

    def start(self) -> RtspSource:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name=f"rtsp-{self.camera_id}", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self.connected = False

    # -- analyser side -----------------------------------------------------------------------

    def latest(self, timeout_s: float = 0.0) -> FrameIn | None:
        """The newest sampled frame not yet handed over, or ``None`` (waits up to ``timeout_s``)."""
        with self._cond:
            if self._slot is None and timeout_s > 0:
                self._cond.wait(timeout_s)
            slot, self._slot = self._slot, None
        if slot is None:
            return None
        ts, image = slot
        state: str | None = None
        metrics: dict[str, float | int] = {}
        if self.quality is not None:
            state, metrics = self.quality.assess(image)
        return FrameIn(
            ts=ts,
            frame_id=f"{self.camera_id}-{ts.strftime('%Y%m%dT%H%M%S%fZ')}",
            image=image,
            camera_state=state,
            quality=dict(metrics),
        )

    def status(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "connected": self.connected,
            "frames": self.frames,
            "dropped": self.dropped,
            "reconnects": self.reconnects,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_error": self.last_error,
        }
