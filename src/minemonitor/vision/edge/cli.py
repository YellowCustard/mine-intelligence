"""Edge command line: fetch weights, build an evaluation clip, run a clip, benchmark a model.

Needs the ``vision`` extra. Usage::

    python -m minemonitor.vision.edge.cli fetch-models [--dest var/models]
    python -m minemonitor.vision.edge.cli make-still-clip [--out var/clips/still_walk.mp4]
    python -m minemonitor.vision.edge.cli bench --model yolox-nano --clip var/clips/still_walk.mp4
    python -m minemonitor.vision.edge.cli run-clip --clip … --core http://localhost:8000 \\
        --site kn-zw-01 --camera-id <id> --user edge-01 --password …

Weights and clips live under ``var/`` (gitignored) and are **fetched with a pinned SHA-256** —
never committed. A file whose hash does not match is deleted, not used.

``make-still-clip`` builds the dev-evaluation clip decided at the 2026-09-28 gate: a
**public-domain** photograph of a person (NASA portrait of astronaut Eileen Collins, as distributed
with scikit-image) moved across a synthetic background into a restricted image-zone. It exercises
the *real* detector → tracker → rules → alarm path without site footage. It is evaluation data
only — never training data — and says nothing about accuracy on real mine footage.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODELS: dict[str, dict[str, Any]] = {
    "yolox-nano": {
        "file": "yolox_nano.onnx",
        "version": "0.1.1rc0",
        "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
        "sha256": "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
        "licence": "Apache-2.0",
    },
    "yolox-s": {
        "file": "yolox_s.onnx",
        "version": "0.1.1rc0",
        "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx",
        "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "licence": "Apache-2.0",
    },
}
STILL = {
    "file": "astronaut.png",
    "url": "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.24.0/skimage/data/astronaut.png",
    "sha256": "",  # pinned on first verified fetch — see fetch()
    "licence": "Public domain (NASA)",
}
# Restricted image-zone used with the still clip (right third of the frame, lower 70%).
STILL_ZONE = [[0.62, 0.3], [0.99, 0.3], [0.99, 0.99], [0.62, 0.99]]


def fetch(url: str, dest: Path, sha256: str) -> Path:
    """Download ``url`` to ``dest`` and verify its SHA-256 (delete on mismatch)."""
    from minemonitor.vision.edge.adapters.yolox_onnx import sha256_of

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:  # noqa: S310
            while chunk := resp.read(1 << 20):
                f.write(chunk)
    got = sha256_of(dest)
    if not sha256:
        # An unpinned artefact is never trusted silently: report the hash so it can be pinned.
        dest.unlink()
        raise SystemExit(f"{dest.name}: no pinned sha256 — observed {got}; pin it in cli.py first")
    if got != sha256:
        dest.unlink()
        raise SystemExit(f"{dest.name}: sha256 {got} != pinned {sha256} — deleted")
    return dest


def cmd_fetch_models(a: argparse.Namespace) -> None:
    for name, m in MODELS.items():
        p = fetch(m["url"], Path(a.dest) / m["file"], m["sha256"])
        print(f"{name}: {p} ok ({m['licence']})")


def make_still_clip(src: Path, out: Path, *, seconds: float = 20.0, fps: float = 5.0) -> Path:
    """Render the evaluation clip: the portrait walks in from the left and stops in the zone."""
    import cv2
    import numpy as np

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {src}")
    w, h = 1280, 720
    person = cv2.resize(img, (340, 340), interpolation=cv2.INTER_AREA)
    rng = np.random.default_rng(3)
    base = np.clip(rng.normal(96, 14, (h, w, 3)), 0, 255).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 3)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        raise SystemExit("OpenCV cannot write mp4v here")
    n = int(seconds * fps)
    walk = int(n * 0.55)
    x0, x1, y = 40, 860, 330
    for i in range(n):
        x = int(x0 + (x1 - x0) * min(i / walk, 1.0))
        frame = base.copy()
        frame[y : y + 340, x : x + 340] = person
        writer.write(frame)
    writer.release()
    return out


def cmd_make_still_clip(a: argparse.Namespace) -> None:
    src = fetch(STILL["url"], Path(a.cache) / STILL["file"], STILL["sha256"])
    out = make_still_clip(src, Path(a.out), seconds=a.seconds, fps=a.fps)
    print(f"clip: {out}  (source: {STILL['url']} — {STILL['licence']}; evaluation only)")


def load_model(name: str, models_dir: str) -> Any:
    from minemonitor.vision.edge.adapters.yolox_onnx import YoloxOnnxModel

    m = MODELS[name]
    return YoloxOnnxModel(
        Path(models_dir) / m["file"],
        model_id=name,
        version=m["version"],
        expected_sha256=m["sha256"],
    )


def bench(model: Any, clip: Path, *, sample_fps: float, max_frames: int) -> dict[str, Any]:
    """Measure CPU inference: fps and per-frame latency percentiles over real decoded frames."""
    import os

    from minemonitor.vision.edge.source import VideoFileSource

    lat: list[float] = []
    dets = 0
    src = VideoFileSource(clip, start=datetime.now(UTC), sample_fps=sample_fps)
    t_start = time.perf_counter()
    for i, f in enumerate(src):
        if i >= max_frames:
            break
        t0 = time.perf_counter()
        dets += len(model.detect(f.image))
        lat.append((time.perf_counter() - t0) * 1000)
    wall = time.perf_counter() - t_start
    lat_sorted = sorted(lat)
    info = model.info()
    return {
        "model": f"{info.id}@{info.version}",
        "input_size": list(info.input_size or ()),
        "frames": len(lat),
        "detections": dets,
        "fps_inference": round(len(lat) / (sum(lat) / 1000), 2) if lat else 0.0,
        "fps_wall_incl_decode": round(len(lat) / wall, 2) if wall else 0.0,
        "latency_ms_p50": round(statistics.median(lat_sorted), 1) if lat else None,
        "latency_ms_p95": round(lat_sorted[int(0.95 * (len(lat) - 1))], 1) if lat else None,
        "cpu_count": os.cpu_count(),
        "backend": info.backend,
        "measured_at": datetime.now(UTC).isoformat(),
    }


def cmd_bench(a: argparse.Namespace) -> None:
    model = load_model(a.model, a.models_dir)
    print(json.dumps(bench(model, Path(a.clip), sample_fps=a.sample_fps, max_frames=a.frames),
                     indent=2))


def cmd_run_clip(a: argparse.Namespace) -> None:
    """Clip → EdgeRuntime(YOLOX) → outbox → a running core, as a device principal."""
    import base64

    from minemonitor.vision.edge.outbox import Outbox
    from minemonitor.vision.edge.publish import HttpPublisher, drain
    from minemonitor.vision.edge.quality import QualityMonitor
    from minemonitor.vision.edge.runtime import EdgeRuntime
    from minemonitor.vision.edge.source import VideoFileSource

    req = urllib.request.Request(
        f"{a.core.rstrip('/')}/api/v1/sites/{a.site}/vision/config",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{a.user}:{a.password}".encode()).decode()
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        config = json.loads(resp.read())
    model = load_model(a.model, a.models_dir)
    outbox = Outbox(a.outbox)
    rt = EdgeRuntime(
        site_id=a.site, node_id=a.node, camera_id=a.camera_id, config=config, model=model,
        outbox=outbox, lab_mode=a.lab,
    )
    publisher = HttpPublisher(a.core, a.user, a.password)
    last = None
    for f in VideoFileSource(a.clip, start=datetime.now(UTC), sample_fps=a.sample_fps,
                             quality=QualityMonitor()):
        for ev in rt.process(f):
            print(f"{ev.ts.isoformat()} {ev.type} zone={ev.zone_id} conf={ev.confidence}")
        last = f.ts
        if rt.stats.frames % 25 == 0:
            drain(outbox, publisher)
    if last is not None:
        rt.close(last)
    print(json.dumps({"frames": rt.stats.frames, "events": rt.stats.events,
                      "infer_ms_mean": round(rt.stats.infer_ms_mean, 1),
                      **drain(outbox, publisher)}))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="minemonitor.vision.edge.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-models")
    f.add_argument("--dest", default="var/models")
    f.set_defaults(fn=cmd_fetch_models)
    m = sub.add_parser("make-still-clip")
    m.add_argument("--cache", default="var/clips")
    m.add_argument("--out", default="var/clips/still_walk.mp4")
    m.add_argument("--seconds", type=float, default=20.0)
    m.add_argument("--fps", type=float, default=5.0)
    m.set_defaults(fn=cmd_make_still_clip)
    for name, fn in (("bench", cmd_bench), ("run-clip", cmd_run_clip)):
        s = sub.add_parser(name)
        s.add_argument("--model", choices=sorted(MODELS), default="yolox-nano")
        s.add_argument("--models-dir", default="var/models")
        s.add_argument("--clip", required=True)
        s.add_argument("--sample-fps", type=float, default=5.0)
        s.set_defaults(fn=fn)
        if name == "bench":
            s.add_argument("--frames", type=int, default=100)
        else:
            s.add_argument("--core", required=True)
            s.add_argument("--site", required=True)
            s.add_argument("--camera-id", required=True)
            s.add_argument("--node", default="edge-01")
            s.add_argument("--user", required=True)
            s.add_argument("--password", required=True)
            s.add_argument("--outbox", default="var/outbox.sqlite")
            s.add_argument("--lab", action="store_true")
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
