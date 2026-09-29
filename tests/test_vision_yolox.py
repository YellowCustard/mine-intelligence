"""Edge perception with the ``vision`` extra: YOLOX maths, frame quality, video source, real smoke.

Skipped cleanly when the extra is not installed (the default CI job). The maths tests need no
weights. The real-model tests run only when the SHA-pinned weights (and the public-domain still)
are present under ``var/`` — fetched by ``python -m minemonitor.vision.edge.cli fetch-models`` /
``make-still-clip``; they are never committed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
pytest.importorskip("onnxruntime")

from sqlalchemy.orm import Session  # noqa: E402

from minemonitor.vision.adapters.base import Detection  # noqa: E402
from minemonitor.vision.edge.adapters.yolox_onnx import (  # noqa: E402
    grid_decode,
    letterbox,
    looks_decoded,
    nms,
    postprocess,
)
from minemonitor.vision.edge.quality import QualityMonitor  # noqa: E402
from minemonitor.vision.edge.source import VideoFileSource  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NANO = REPO / "var" / "models" / "yolox_nano.onnx"
STILL = REPO / "var" / "clips" / "astronaut.png"
T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


# -- YOLOX pre/post-processing (no weights) ---------------------------------------------------


def test_letterbox_keeps_aspect_and_pads_114() -> None:
    img = np.full((100, 200, 3), 50, np.uint8)
    chw, r = letterbox(img, (416, 416))
    assert chw.shape == (3, 416, 416) and chw.dtype == np.float32
    assert r == pytest.approx(2.08)
    assert chw[0, 0, 0] == 50 and chw[0, 415, 415] == 114  # image top-left, pad bottom-right


def test_grid_decode_matches_official_formula() -> None:
    rows = 52 * 52 + 26 * 26 + 13 * 13
    raw = np.zeros((rows, 85), np.float32)
    k = 5 * 52 + 10  # stride-8 cell (x=10, y=5)
    raw[k, :4] = [0.5, 0.5, math.log(4), math.log(8)]
    out = grid_decode(raw, (416, 416))
    assert out[k, :4] == pytest.approx([84.0, 44.0, 32.0, 64.0], rel=1e-5)
    assert not looks_decoded(raw) and looks_decoded(out)
    with pytest.raises(ValueError):
        grid_decode(raw[:-1], (416, 416))


def test_nms_suppresses_overlaps() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    assert nms(boxes, np.array([0.9, 0.8, 0.7]), 0.45) == [0, 2]


def test_postprocess_person_only_normalised_and_nmsed() -> None:
    d = np.zeros((3, 85), np.float32)
    # two overlapping people and a car, in input pixels (ratio 0.5 ⇒ image is 2× input)
    d[0, :5] = [100, 100, 40, 80, 0.9]
    d[0, 5] = 0.95
    d[1, :5] = [102, 101, 40, 80, 0.9]
    d[1, 5] = 0.80
    d[2, :5] = [300, 300, 60, 40, 0.9]
    d[2, 5 + 2] = 0.99  # COCO class 2 = car: not mapped
    dets = postprocess(
        d, ratio=0.5, image_wh=(832, 832), conf_thr=0.3, nms_thr=0.45, class_map={0: "person"}
    )
    assert len(dets) == 1 and isinstance(dets[0], Detection)
    (p,) = dets
    assert p.cls == "person" and p.confidence == pytest.approx(0.855, abs=1e-3)
    assert p.bbox == pytest.approx((80 / 416, 60 / 416, 120 / 416, 140 / 416), rel=1e-4)


# -- quality + source --------------------------------------------------------------------------


def test_quality_states() -> None:
    q = QualityMonitor()
    assert q.assess(np.zeros((360, 640, 3), np.uint8))[0] == "obstructed"
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
    assert QualityMonitor().assess(noise)[0] == "ok"
    assert QualityMonitor().assess(np.full((360, 640, 3), 128, np.uint8))[0] == "defocused"
    dim = (noise // 12).astype(np.uint8)
    assert QualityMonitor().assess(dim)[0] == "dark"
    frozen = QualityMonitor()
    states = [frozen.assess(noise)[0] for _ in range(30)]
    assert states[0] == "ok" and states[-1] == "no_signal"


def test_video_source_samples_by_time(tmp_path: Path) -> None:
    path = tmp_path / "c.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 25.0, (160, 90))
    for i in range(100):  # 4 s at 25 fps
        w.write(np.full((90, 160, 3), i % 255, np.uint8))
    w.release()
    frames = list(VideoFileSource(path, start=T0, sample_fps=5, quality=QualityMonitor()))
    assert len(frames) == 20
    assert frames[1].ts - frames[0].ts == timedelta(seconds=0.2)
    assert frames[0].frame_id == "c-000000" and frames[0].camera_state is not None
    with pytest.raises(ValueError):
        VideoFileSource(path, start=datetime(2026, 1, 1), sample_fps=5)  # naive start


# -- real weights (only when fetched) -------------------------------------------------------------

needs_weights = pytest.mark.skipif(not NANO.exists(), reason="YOLOX weights not fetched")


@needs_weights
def test_real_yolox_nano_on_blank_and_person() -> None:
    from minemonitor.vision.edge.adapters.yolox_onnx import YoloxOnnxModel
    from minemonitor.vision.edge.cli import MODELS

    model = YoloxOnnxModel(NANO, expected_sha256=MODELS["yolox-nano"]["sha256"])
    assert model.info().licence == "Apache-2.0" and model.input_size == (416, 416)
    assert model.detect(np.full((720, 1280, 3), 96, np.uint8)) == []
    if STILL.exists():
        dets = model.detect(cv2.imread(str(STILL)))
        assert dets and dets[0].cls == "person" and dets[0].confidence > 0.5


@needs_weights
def test_wrong_hash_is_refused(tmp_path: Path) -> None:
    from minemonitor.vision.edge.adapters.yolox_onnx import YoloxOnnxModel

    with pytest.raises(ValueError, match="sha256 mismatch"):
        YoloxOnnxModel(NANO, expected_sha256="0" * 64)


@pytest.mark.skipif(not (NANO.exists() and STILL.exists()), reason="weights/still not fetched")
def test_real_model_end_to_end_one_alarm(db_session: Session, tmp_path: Path) -> None:
    """V3 smoke on stills: real YOLOX → tracker → rule → core → exactly one alarm."""
    from minemonitor.cameras.service import create_camera
    from minemonitor.events.repository import list_events
    from minemonitor.storage.models import Zone
    from minemonitor.vision import service
    from minemonitor.vision.edge.adapters.yolox_onnx import YoloxOnnxModel
    from minemonitor.vision.edge.cli import MODELS, STILL_ZONE, make_still_clip
    from minemonitor.vision.edge.outbox import Outbox
    from minemonitor.vision.edge.publish import drain
    from minemonitor.vision.edge.runtime import EdgeRuntime
    from tests.conftest import DEVICE, make_client
    from tests.test_vision_pipeline import _FAR_POLYGON, Published

    site = "kn-zw-01"
    db_session.add(
        Zone(
            zone_id="gr",
            site_id=site,
            name="Gold Room",
            kind="restricted",
            geometry=_FAR_POLYGON,
            rules={},
        )
    )
    cam = create_camera(db_session, site_id=site, name="CAM-STILL", now=T0)
    cam.image_zones = [{"zone_id": "gr", "zone_version": "v1", "polygon": STILL_ZONE}]
    service.install_default_rules(db_session, site, author="t", at=T0 - timedelta(days=1))
    db_session.commit()

    clip = make_still_clip(STILL, tmp_path / "walk.mp4", seconds=20, fps=5)
    model = YoloxOnnxModel(NANO, expected_sha256=MODELS["yolox-nano"]["sha256"])
    ob = Outbox(tmp_path / "o.sqlite")
    rt = EdgeRuntime(
        site_id=site,
        node_id="edge-1",
        camera_id=cam.id,
        config=service.edge_config(db_session, site, at=T0),
        model=model,
        outbox=ob,
    )
    last = T0
    for f in VideoFileSource(clip, start=T0, sample_fps=5, quality=QualityMonitor()):
        rt.process(f)
        last = f.ts
    rt.close(last)
    assert rt.stats.detections >= rt.stats.frames * 0.9  # the person is found in ~every frame
    drain(ob, Published(make_client(db_session, DEVICE)))
    alarms = list_events(db_session, site_id=site)
    assert [a.type for a in alarms] == ["zone_breach"], [a.summary for a in alarms]
    assert alarms[0].detail["provenance"] == "inferred"
