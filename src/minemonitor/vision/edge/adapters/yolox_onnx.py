"""YOLOX (Megvii, Apache-2.0 code + weights) via ONNX Runtime (MIT) on CPU — the edge detector.

Needs the optional ``vision`` extra (NumPy, ONNX Runtime). Pre/post-processing mirrors the official
YOLOX ONNX demo (``demo/ONNXRuntime/onnx_inference.py`` in Megvii-BaseDetection/YOLOX) so the
official release weights behave exactly as published:

- **Input:** a BGR ``uint8`` frame (OpenCV's native order), letterboxed to the model size with
  pad value 114, top-left aligned, **no** mean/std normalisation, CHW ``float32``.
- **Output:** ``(1, N, 5 + C)`` rows of ``[cx, cy, w, h, objectness, class scores…]`` in *grid*
  space; decoded over strides 8/16/32 as ``xy = (out + grid) · stride``, ``wh = exp(out) · stride``.
  Exports that already decode in-graph are detected and left alone.
- **Scores:** ``objectness × class score``; class-wise NMS; boxes un-letterboxed and normalised to
  ``[0, 1]``.

Only COCO class 0 → ``person`` is mapped by default. COCO "truck" is **not** mapped to
``haul_truck``: a street-truck detector is not a mining-truck detector, and claiming otherwise would
overstate the model. Mining classes arrive with fine-tuning (gated, ``VISION_TRAINING.md``).

The adapter returns :class:`Detection` only — class, box, confidence. Never identity.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from minemonitor.vision.adapters.base import Detection, ModelInfo, VisionModel

try:  # the edge extra; the core and default CI never import this module
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("YOLOX adapter needs the 'vision' extra: uv sync --extra vision") from exc

STRIDES = (8, 16, 32)
PAD_VALUE = 114
DEFAULT_CLASS_MAP: dict[int, str] = {0: "person"}


def letterbox(img: Any, size: tuple[int, int]) -> tuple[Any, float]:
    """Resize keeping aspect, pad to ``size`` (h, w) with 114, return CHW float32 + scale ratio."""
    import cv2

    h, w = size
    ih, iw = img.shape[:2]
    r = min(h / ih, w / iw)
    nh, nw = int(ih * r), int(iw * r)
    padded = np.full((h, w, 3), PAD_VALUE, dtype=np.uint8)
    padded[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32), r


def grid_decode(raw: Any, size: tuple[int, int]) -> Any:
    """Decode grid-space outputs ``(N, 5+C)`` to input-pixel ``cx, cy, w, h`` (copy)."""
    h, w = size
    grids, strides = [], []
    for s in STRIDES:
        hs, ws = h // s, w // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((hs * ws, 1), s))
    grid = np.concatenate(grids, 0)
    stride = np.concatenate(strides, 0)
    if grid.shape[0] != raw.shape[0]:
        raise ValueError(f"output has {raw.shape[0]} rows; strides imply {grid.shape[0]}")
    out = raw.astype(np.float32, copy=True)
    out[:, :2] = (out[:, :2] + grid) * stride
    out[:, 2:4] = np.exp(out[:, 2:4]) * stride
    return out


def looks_decoded(raw: Any) -> bool:
    """Grid-space w/h are small log values; decoded w/h are pixel sizes."""
    return bool(raw.shape[0] and float(np.max(raw[:, 2:4])) > 20.0)


def nms(boxes: Any, scores: Any, iou_thr: float) -> list[int]:
    """Greedy NMS on ``xyxy`` boxes; indices kept, highest score first."""
    order = scores.argsort()[::-1]
    keep: list[int] = []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(xx2 - xx1, 0) * np.maximum(yy2 - yy1, 0)
        iou = inter / np.maximum(areas[i] + areas[order[1:]] - inter, 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


def postprocess(
    decoded: Any,
    *,
    ratio: float,
    image_wh: tuple[int, int],
    conf_thr: float,
    nms_thr: float,
    class_map: dict[int, str],
) -> list[Detection]:
    """Decoded ``(N, 5+C)`` → normalised, class-mapped, NMS'd :class:`Detection`s."""
    iw, ih = image_wh
    obj = decoded[:, 4:5]
    cls_scores = decoded[:, 5:] * obj
    cls_ids = cls_scores.argmax(1)
    scores = cls_scores[np.arange(len(cls_ids)), cls_ids]
    wanted = np.isin(cls_ids, list(class_map)) & (scores >= conf_thr)
    if not wanted.any():
        return []
    d, s, c = decoded[wanted], scores[wanted], cls_ids[wanted]
    cx, cy, bw, bh = d[:, 0], d[:, 1], d[:, 2], d[:, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1) / ratio
    out: list[Detection] = []
    for cid in np.unique(c):
        m = c == cid
        for k in nms(boxes[m], s[m], nms_thr):
            x1, y1, x2, y2 = boxes[m][k]
            nx1, ny1 = max(0.0, float(x1) / iw), max(0.0, float(y1) / ih)
            nx2, ny2 = min(1.0, float(x2) / iw), min(1.0, float(y2) / ih)
            if nx2 - nx1 <= 1e-4 or ny2 - ny1 <= 1e-4:
                continue  # degenerate after clipping to the frame
            out.append(
                Detection(class_map[int(cid)], round(float(s[m][k]), 4), (nx1, ny1, nx2, ny2))
            )
    out.sort(key=lambda det: -det.confidence)
    return out


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class YoloxOnnxModel(VisionModel):
    """YOLOX behind the vendor-isolation boundary. CPU by default; GPU is a provider swap later."""

    def __init__(
        self,
        path: str | Path,
        *,
        model_id: str = "yolox-nano",
        version: str = "0.1.1rc0",
        expected_sha256: str | None = None,
        conf_thr: float = 0.35,
        nms_thr: float = 0.45,
        class_map: dict[int, str] | None = None,
        threads: int | None = None,
    ) -> None:
        import onnxruntime as ort

        self.path = Path(path)
        self.sha256 = sha256_of(self.path)
        if expected_sha256 and self.sha256 != expected_sha256:
            raise ValueError(f"{self.path.name}: sha256 mismatch — refusing unverified weights")
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self._session = ort.InferenceSession(
            str(self.path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        self.input_size = (int(inp.shape[2]), int(inp.shape[3]))
        self.model_id = model_id
        self.version = version
        self.conf_thr = conf_thr
        self.nms_thr = nms_thr
        self.class_map = class_map or dict(DEFAULT_CLASS_MAP)

    def info(self) -> ModelInfo:
        return ModelInfo(
            id=self.model_id,
            version=self.version,
            task="detect",
            licence="Apache-2.0",
            classes=tuple(sorted(set(self.class_map.values()))),
            input_size=self.input_size,
            backend="onnxruntime-cpu",
            extra={"sha256": self.sha256, "conf_thr": self.conf_thr, "nms_thr": self.nms_thr},
        )

    def detect(self, frame: Any) -> list[Detection]:
        if not hasattr(frame, "shape") or frame.ndim != 3 or frame.shape[2] != 3:
            raise TypeError("YoloxOnnxModel expects a BGR uint8 frame of shape (h, w, 3)")
        chw, ratio = letterbox(frame, self.input_size)
        raw = self._session.run(None, {self._input_name: chw[None]})[0][0]
        decoded = raw if looks_decoded(raw) else grid_decode(raw, self.input_size)
        return postprocess(
            decoded,
            ratio=ratio,
            image_wh=(int(frame.shape[1]), int(frame.shape[0])),
            conf_thr=self.conf_thr,
            nms_thr=self.nms_thr,
            class_map=self.class_map,
        )
