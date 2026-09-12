# Mine Monitor — Vision Model Catalog & Recommended Stack

**Status: research and recommendation for review. No model is a dependency yet.**

This covers Phase 2 (model inventory) and Phase 4 (recommended stack). Licensing is
summarised here and is authoritative in `VISION_MODEL_LICENSES.md`; where the two ever
disagree, the license document wins.

## The governing constraint (read first)

`CLAUDE.md` and `LICENCES.md` are absolute: **Apache-2.0 / MIT / BSD only** for anything
that ships, and **Ultralytics YOLO is AGPL-3.0 and must not enter this codebase or the
vision repo.** AGPL's network-use clause is a commercial trap for a product eigenstate
sells (including SaaS). This single rule removes the most "popular" default (Ultralytics
YOLOv5/v8/v11, and Ultralytics-packaged RT-DETR/YOLO-World/YOLOE/FastSAM) from the
production shortlist. Popularity is not a selection criterion; a clean commercial licence
is a gate.

Two consequences worth stating up front:
1. Where an algorithm is good but its most popular *packaging* is AGPL (RT-DETR inside
   Ultralytics; ByteTrack/BoT-SORT inside Ultralytics), we take the algorithm from its
   **original permissively-licensed implementation**, not the Ultralytics package.
2. A permissively-licensed model repo can still ship **non-commercial weights**
   (e.g. YOLO-NAS, some Depth-Anything checkpoints). Code licence ≠ weights licence —
   both are checked, per model, per release.

Licence legend below: ✅ commercial-OK (Apache/MIT/BSD, code+weights) · ⚠️ conditional
(check weights, or copyleft/attribution) · ⛔ do not ship (AGPL / non-commercial).

---

## 1. Object detection

| Model | Provider | Code licence | Weights | Size / speed | Edge | ONNX | TensorRT | Train/finetune | Mining fit | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| **RT-DETR / RT-DETRv2** | Baidu (PaddlePaddle) + PyTorch port (`lyuwenyu/RT-DETR`) | Apache-2.0 ✅ | Apache ✅ | R18–R101; real-time on GPU | GPU-good | yes | yes | yes | strong: anchor-free, robust to scale (near/far trucks) | **✅ default (GPU-accuracy)** |
| **RF-DETR** | Roboflow | Apache-2.0 ✅ | Apache ✅ | small/base; real-time | GPU-good | yes | yes | yes | strong; modern, good small-object | **✅ strong alternate** |
| **D-FINE** | open (`Peterande/D-FINE`) | Apache-2.0 ✅ | Apache ✅ (check per release) | S–X; SOTA-adjacent real-time | GPU-good | yes | partial | yes | strong regression-based DETR | ⚠️ track; promising alternate |
| **YOLOX** | Megvii | Apache-2.0 ✅ | Apache ✅ | Nano→X; fast on CPU too | **CPU/edge-good** | yes | yes | yes | good; mature, anchor-free | **✅ default (CPU-small / edge)** |
| **PP-YOLOE / PP-YOLOE+** | Baidu (PaddleDetection) | Apache-2.0 ✅ | Apache ✅ | S–X | GPU-good | via Paddle→ONNX | yes | yes | good | ⚠️ viable; Paddle toolchain overhead |
| **RTMDet** | OpenMMLab (MMDetection) | Apache-2.0 ✅ | Apache ✅ | tiny→X | GPU-good | yes | yes | yes | good; strong train ecosystem | **✅ viable (via MMDet)** |
| Ultralytics YOLOv5/v8/v11 | Ultralytics | **AGPL-3.0 ⛔** | AGPL ⛔ | excellent all-round | great | yes | yes | yes | excellent *technically* | **⛔ forbidden (licence)** |
| YOLOv7 | (`WongKinYiu`) | **GPL-3.0 ⚠️** | GPL | fast | good | yes | yes | yes | good | ⚠️ copyleft; avoid for shipped product |
| YOLO-NAS | Deci / `super-gradients` | Apache-2.0 (code) but **weights non-commercial ⛔** | ⛔ | fast | good | yes | yes | yes | good | **⛔ weights not commercial** |
| DETR / Deformable-DETR | Meta / SenseTime | Apache-2.0 ✅ | Apache ✅ | slower (not real-time) | poor | yes | partial | yes | baseline only | ⚠️ not real-time |

**Detection takeaways**
- Two permissive defaults cover the profiles: **YOLOX** (CPU-small / edge) and
  **RT-DETR / RT-DETRv2** (GPU-balanced/accuracy). Both are Apache-2.0 in code *and*
  weights, both export to ONNX and TensorRT, both fine-tune cleanly.
- **RF-DETR** and **RTMDet** are the ready alternates and keep us multi-vendor (the
  adapter interface means adopting one is an adapter, not a rewrite).
- The Ultralytics family is technically excellent and explicitly **out** on licence —
  including any RT-DETR/YOLO-World *packaged inside* Ultralytics. Take RT-DETR from its
  original Apache repo instead.

---

## 2. Segmentation

Used **offline / for annotation assist and stockpile-area analysis**, not per-frame in
the real-time hot path (masks are expensive; detection + tracking carry the operational
load).

| Model | Provider | Code | Weights | Notes | Verdict |
|---|---|---|---|---|---|
| **SAM 2** | Meta | Apache-2.0 ✅ | Apache ✅ | promptable image+video seg; great for annotation & rare-object masks | **✅ annotation / offline** |
| **MobileSAM** | (`ChaoningZhang`) | Apache-2.0 ✅ | Apache ✅ | lightweight SAM; edge-capable | **✅ light seg** |
| SAM (v1) | Meta | Apache-2.0 ✅ | Apache ✅ | original; heavier than SAM2 | ✅ ok |
| YOLOX-Seg / RTMDet-Ins | Megvii / OpenMMLab | Apache-2.0 ✅ | Apache ✅ | instance seg in a permissive detector family | ✅ if inline seg needed |
| **FastSAM** | Ultralytics-based | **AGPL-3.0 ⛔** | ⛔ | fast, but AGPL packaging | **⛔ forbidden** |
| YOLOv8-seg | Ultralytics | **AGPL-3.0 ⛔** | ⛔ | — | **⛔ forbidden** |

**Segmentation takeaway:** **SAM2 / MobileSAM** (Apache-2.0) for annotation bootstrapping
and occasional area/stockpile masks. If inline instance segmentation is ever needed in
production, use **RTMDet-Ins / YOLOX-Seg**, never the Ultralytics or FastSAM route.

---

## 3. Tracking (a separate layer — never the detector's job)

Trackers are algorithms; take them from their original permissive implementations, not
from an AGPL bundle.

| Tracker | Type | Licence | Needs ReID model? | Strength | Verdict |
|---|---|---|---|---|---|
| **ByteTrack** | motion (Kalman + IoU, keeps low-conf) | **MIT ✅** | no | robust default; handles missed detections | **✅ default** |
| **OC-SORT** | motion (observation-centric) | **MIT ✅** | no | better through occlusion / non-linear motion | **✅ occlusion cases** |
| BoT-SORT | motion + optional appearance ReID | **MIT ✅** (code) | optional (⚠️ check ReID weights) | best id-consistency *with* ReID | ⚠️ use only if ReID licence clears |
| SORT | motion (basic) | GPL-3.0 ⚠️ | no | baseline | ⚠️ avoid (copyleft); ByteTrack supersedes |
| DeepSORT | motion + appearance | MIT (code) ⚠️ | yes (⚠️ classic ReID weights often restricted) | legacy | ⚠️ ReID weight licence risk |

**Tracking takeaway:** **ByteTrack (MIT)** default; **OC-SORT (MIT)** where occlusion is
heavy (dust, crossing equipment). Both are appearance-free, so no ReID-weight licence
exposure. Add BoT-SORT + a *permissively-licensed* ReID model only if id-consistency in
dense scenes demands it, and only after the ReID weights clear the licence gate.

---

## 4. Open-vocabulary detection (dev/bootstrap tool — not a production dependency)

Useful to **pre-label** a mining dataset fast ("detect: haul truck, excavator, person")
before we have a fine-tuned model. Kept strictly as an annotation accelerator.

| Model | Provider | Code | Weights | Verdict |
|---|---|---|---|---|
| **Grounding DINO** | IDEA Research | Apache-2.0 ✅ | Apache ✅ | **✅ dev pre-labelling** |
| **OWLv2 / OWL-ViT** | Google (via HF Transformers) | Apache-2.0 ✅ | Apache ✅ | **✅ dev pre-labelling** |
| YOLO-World | Tencent (orig.) | **GPL-3.0 ⚠️**; Ultralytics pkg **AGPL ⛔** | ⚠️/⛔ | ⚠️ dev-only, prefer the above |
| YOLOE | Ultralytics | **AGPL-3.0 ⛔** | ⛔ | **⛔ forbidden** |

**Open-vocab takeaway:** use **Grounding DINO** or **OWLv2** (both Apache-2.0) *offline*
to bootstrap annotations (see `MINING_VISION_DATASET_STRATEGY.md`); auto-labels are
reviewed by a human before training. Do **not** wire an open-vocab model into the
real-time production pipeline — they are large, slow, and unnecessary once a fine-tuned
detector exists.

---

## 5. Depth / spatial understanding (include only if it earns its place)

Monocular metric depth is **not reliable enough to base a safety-critical proximity
alarm on**. For ground-plane distance (person↔equipment), **camera calibration +
homography** to the ground plane is more accurate, cheaper, and explainable — and is the
recommended default (see `VISION_GPS_FUSION.md` / `VISION_ARCHITECTURE.md` §7). Learned
depth is a *relative* cue at most.

| Model | Provider | Code | Weights | Verdict |
|---|---|---|---|---|
| **Depth Anything V2 (Small)** | (`DepthAnything`) | Apache-2.0 ✅ | **Small: Apache ✅**; larger: **CC-BY-NC ⛔** | ⚠️ small-only, relative depth, non-safety |
| ZoeDepth | ISL-org | MIT ✅ | MIT ✅ | ⚠️ metric-ish, still not safety-grade |
| Metric3D / UniDepth | various | check per repo | check | ⚠️ evaluate only if a real need appears |

**Depth takeaway:** default to **homography on a calibrated ground plane** for distance;
treat learned monocular depth as an optional *relative* enhancement (Depth-Anything-V2
**Small** only, Apache weights), never as the basis of a proximity safety decision.

---

## 6. Inference runtime & supporting libraries (all must be permissive)

| Component | Choice | Licence | Notes |
|---|---|---|---|
| Training / research | PyTorch | BSD-3 ✅ | base framework |
| Detector training ecosystem | MMDetection / Detectron2 | Apache-2.0 ✅ | RTMDet, DETR family |
| Portable inference | **ONNX Runtime** | MIT ✅ | **default** cross-platform backend |
| Intel CPU/iGPU edge | OpenVINO | Apache-2.0 ✅ | CPU-small profile |
| NVIDIA edge | TensorRT | **proprietary EULA ⚠️** | runtime redistribution restricted — bundle per NVIDIA terms, do not vendor the SDK into the repo |
| Video decode / CV ops | OpenCV | Apache-2.0 ✅ | ⚠️ avoid GPL `ffmpeg` builds; use LGPL/BSD codecs |
| Detection/annotation utils | `supervision` (Roboflow) | MIT ✅ | boxes, zones, annotators |
| Tracking impls | ByteTrack / OC-SORT (orig.) | MIT ✅ | §3 |

**Runtime takeaway:** **ONNX Runtime (MIT)** is the default portable backend; **OpenVINO**
for Intel edge; **TensorRT** only on NVIDIA hardware with its EULA respected (it is a
runtime we deploy on, not source we ship). Everything else is Apache/MIT/BSD. Watch the
transitive trap: an AGPL/GPL codec or utility pulled in by a "permissive" wrapper still
poisons the ship — pin and audit (see `VISION_MODEL_LICENSES.md`).

---

## 7. Recommended default stack

Chosen for a clean commercial licence first, then mining fitness, then edge practicality,
and always replaceable behind the adapter interface (`VISION_ARCHITECTURE.md` §5).

```
                     Mine Monitor Vision (edge service)
                                  │
                ┌─────────────────┴─────────────────┐
                │                                     │
           Detection                            Segmentation (offline/annotation)
   YOLOX-S/Nano  (CPU-small)                    SAM2 / MobileSAM  (Apache-2.0)
   RT-DETRv2-R50 (GPU-balanced/accuracy)                 │
   [alt: RF-DETR, RTMDet]                                │
                └─────────────────┬─────────────────────┘
                                  │
                             Tracking
                   ByteTrack (MIT) · OC-SORT (MIT, occlusion)
                                  │
                          Spatial reasoning
              calibrated homography (ground distance) · image-zones
              [optional relative cue: Depth-Anything-V2-Small, Apache]
                                  │
                         Temporal reasoning
              per-track windowed state · debounce/hysteresis
                                  │
                       Mining activity engine (rules)
                                  │
                    vision.observation.v1 / vision.operational_event.v1
                                  │
                             Mine Monitor core
       Runtime: ONNX Runtime (default) · OpenVINO (Intel) · TensorRT (NVIDIA, EULA)
       Dev-only bootstrap: Grounding DINO / OWLv2 (Apache) for pre-labelling
```

| Role | Default | Licence | Alternates (all permissive) |
|---|---|---|---|
| Detection — edge/CPU | **YOLOX-S / -Nano** | Apache-2.0 | RTMDet-tiny |
| Detection — GPU | **RT-DETRv2 (R50)** | Apache-2.0 | RF-DETR, D-FINE, RTMDet |
| Tracking | **ByteTrack** | MIT | OC-SORT, BoT-SORT (+cleared ReID) |
| Segmentation (offline) | **SAM2 / MobileSAM** | Apache-2.0 | RTMDet-Ins / YOLOX-Seg |
| Ground distance | **calibrated homography** | n/a (our code) | Depth-Anything-V2-Small (relative) |
| Open-vocab (dev only) | **Grounding DINO** | Apache-2.0 | OWLv2 |
| Inference runtime | **ONNX Runtime** | MIT | OpenVINO (Intel), TensorRT (NVIDIA, EULA) |

**Do not hard-code any of these into the platform core.** The core sees only
`vision.*.v1`; each model sits behind an adapter and is swappable per profile and per
site. This table is the *starting* default, chosen to be replaceable — not a vendor
commitment.

Selection rule for any future addition: **licence gate first** (Apache/MIT/BSD, code
*and* weights, commercial + SaaS + redistribution OK), then measured mining accuracy on
our validation set (`VISION_TRAINING.md`), then edge cost. A model never reaches
`production` status in the registry until all three pass and its licence line is in
`VISION_MODEL_LICENSES.md`.
