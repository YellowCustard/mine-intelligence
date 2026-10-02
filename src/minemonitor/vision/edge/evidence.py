"""Evidence stills for operational events: a bounded edge ring, optionally forwarded to MinIO.

When a rule concludes an operational event, the node keeps **one still** of the frame that
confirmed it, so a supervisor can see *why* without video ever crossing the link
(``docs/VISION_BUILD_GATE.md`` §4.6). The event carries only a pointer in ``evidence.clip_ref``:

- ``edge`` mode (default): the still stays in the node's local ring buffer —
  ``edge://<node>/<camera>/<event_id>.jpg``. Nothing leaves the box.
- ``s3`` mode (opt-in): the still is also queued for upload to the site's object store and the
  event points at its final location — ``s3://<bucket>/<site>/vision/<camera>/<date>/<event_id>.jpg``.
  Uploads are store-and-forward like the outbox: a dead link delays them, never blocks detection.
  Keys start with the site, so a site's evidence is exportable and deletable by prefix.
- ``off``: no stills; ``clip_ref`` stays null.

The reference is deterministic from the event id, so a replayed event names the same object. A
reference may outlive its still (ring pruning, retention); consumers treat a missing object as
"expired", never as an error.

Stills show people — personal data under the CDPA (not biometric templates; nothing here derives
one). The ring is bounded by count and age, and is the edge's evidence retention class.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from minemonitor.vision.edge.s3 import S3Error, S3Uploader

log = logging.getLogger("minemonitor.vision.edge.evidence")

Mode = Literal["off", "edge", "s3"]
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def jpeg_encoder(max_width: int = 960, quality: int = 80) -> Callable[[Any], bytes]:
    """Return an OpenCV JPEG encoder that downsizes wide frames (needs the ``vision`` extra)."""

    def encode(image: Any) -> bytes:
        import cv2

        h, w = image.shape[:2]
        if w > max_width:
            image = cv2.resize(
                image, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA
            )
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("JPEG encode failed")
        return bytes(buf.tobytes())

    return encode


def write_atomic(path: Path, data: bytes) -> None:
    """Crash-safe write: a power cut leaves the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class EvidenceStore:
    """Keep and forward evidence stills. One per node; shared by its cameras."""

    def __init__(
        self,
        root: str | Path,
        *,
        site_id: str,
        node_id: str,
        mode: Mode = "edge",
        uploader: S3Uploader | None = None,
        encoder: Callable[[Any], bytes] | None = None,
        max_items: int = 500,
        max_age: timedelta = timedelta(days=7),
    ) -> None:
        if mode == "s3" and uploader is None:
            raise ValueError("evidence mode 's3' needs an uploader")
        self.root = Path(root)
        self.site_id = site_id
        self.node_id = node_id
        self.mode: Mode = mode
        self.uploader = uploader
        self._encode = encoder
        self.max_items = max_items
        self.max_age = max_age
        self.pending_dir = self.root / "pending"
        self.kept_dir = self.root / "kept"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _key(self, camera_id: str, event_id: str, ts: datetime) -> str:
        day = ts.astimezone(UTC).strftime("%Y/%m/%d")
        return f"{_SAFE.sub('-', self.site_id)}/vision/{_SAFE.sub('-', camera_id)}/{day}/" + (
            f"{_SAFE.sub('-', event_id)}.jpg"
        )

    def save(self, *, camera_id: str, event_id: str, ts: datetime, image: Any) -> str | None:
        """Keep a still for ``event_id`` and return its reference (``None`` when off or failed).

        Never raises into the frame loop: a still that cannot be encoded or written costs the
        event its picture, not the event itself.
        """
        if not self.enabled or image is None:
            return None
        encode = self._encode or jpeg_encoder()
        try:
            data = encode(image)
        except Exception as exc:  # noqa: BLE001 — any encoder failure just drops the still
            log.warning("evidence encode failed", extra={"event_id": event_id, "error": str(exc)})
            return None
        key = self._key(camera_id, event_id, ts)
        local = self.kept_dir / key
        try:
            write_atomic(local, data)
            if self.mode == "s3":
                marker = self.pending_dir / (key + ".pending")
                write_atomic(marker, b"")
        except OSError as exc:
            log.warning("evidence write failed", extra={"event_id": event_id, "error": str(exc)})
            return None
        if self.mode == "s3":
            assert self.uploader is not None
            return self.uploader.uri(key)
        return f"edge://{self.node_id}/{_SAFE.sub('-', camera_id)}/{_SAFE.sub('-', event_id)}.jpg"

    def upload_pending(self, *, limit: int = 50) -> dict[str, int]:
        """Forward queued stills to the object store; stop at the first transport failure."""
        if self.mode != "s3" or self.uploader is None or not self.pending_dir.exists():
            return {"uploaded": 0, "pending": 0}
        markers = sorted(self.pending_dir.rglob("*.pending"))
        uploaded = 0
        for marker in markers[:limit]:
            key = marker.relative_to(self.pending_dir).as_posix()[: -len(".pending")]
            local = self.kept_dir / key
            if not local.exists():  # pruned before it could be sent: the reference is "expired"
                marker.unlink(missing_ok=True)
                continue
            try:
                self.uploader.put(key, local.read_bytes(), content_type="image/jpeg")
            except S3Error as exc:
                log.warning("evidence upload deferred", extra={"error": str(exc)})
                break
            marker.unlink(missing_ok=True)
            uploaded += 1
        left = sum(1 for _ in self.pending_dir.rglob("*.pending"))
        return {"uploaded": uploaded, "pending": left}

    def prune(self, now: datetime | None = None) -> int:
        """Bound the ring: drop stills past ``max_age``, then the oldest beyond ``max_items``."""
        if not self.kept_dir.exists():
            return 0
        now = now or datetime.now(UTC)
        cutoff = (now - self.max_age).timestamp()
        files = sorted(self.kept_dir.rglob("*.jpg"), key=lambda p: p.stat().st_mtime)
        doomed = [p for p in files if p.stat().st_mtime < cutoff]
        survivors = [p for p in files if p.stat().st_mtime >= cutoff]
        if len(survivors) > self.max_items:
            doomed += survivors[: len(survivors) - self.max_items]
        for p in doomed:
            p.unlink(missing_ok=True)
        return len(doomed)
