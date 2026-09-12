# Mine Monitor — Vision Model & Dataset Licence Audit

**Status: licence analysis for review. Mandatory gate — no model, dataset, or weight
reaches `production` status in the model registry until its row here is green.**

This extends `LICENCES.md` to the vision domain. It is authoritative for vision model
licensing; `VISION_MODEL_CATALOG.md` summarises, this decides.

## Policy (from `CLAUDE.md` / `LICENCES.md`, restated so it travels with this file)

Mine Monitor is a **commercial product** eigenstate sells, deployable as **SaaS** and
**on-premise**. Therefore:

- **Ship only Apache-2.0 / MIT / BSD** (code *and* weights).
- **AGPL-3.0 is forbidden.** Its network-use clause obliges source disclosure to remote
  users of a networked product — a licensing trap for what we sell. **Ultralytics YOLO
  (v5/v8/v11) and anything packaged by Ultralytics (including its RT-DETR, YOLO-World,
  YOLOE, FastSAM builds) is AGPL-3.0 and must not enter this codebase or the vision
  repo.**
- **GPL-3.0** copyleft is avoided for shipped components (linking/derivative risk).
- **Non-commercial** weights (CC-BY-NC, research-only) are forbidden regardless of code
  licence.
- **Code licence ≠ weights licence ≠ training-data licence.** All three are audited,
  **per model and per release** — licences change between versions.
- **Transitive dependencies count.** A permissive wrapper that pulls a GPL/AGPL codec or
  ReID model poisons the ship; the audit follows the dependency tree, not just the top
  package.

## The eight commercial questions (answered for every candidate below)

1. Commercial use by eigenstate? 2. Embed in a commercial product? 3. Redistribute the
weights? 4. Fine-tune commercially? 5. Attribution required? 6. Separate commercial
licence required? 7. SaaS restrictions? 8. On-prem restrictions?

---

## 1. Detection models

| Model | Code | Weights | 1 Comm | 2 Embed | 3 Redist wts | 4 Finetune | 5 Attrib | 6 Comm licence | 7 SaaS | 8 On-prem | Ship? |
|---|---|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **YOLOX** | Apache-2.0 | Apache-2.0 | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | **✅** |
| **RT-DETR / v2** (orig. Apache repo) | Apache-2.0 | Apache-2.0 | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | **✅** |
| **RF-DETR** (Roboflow) | Apache-2.0 | Apache-2.0 | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | **✅** |
| **RTMDet** (MMDet) | Apache-2.0 | Apache-2.0 | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | **✅** |
| **D-FINE** | Apache-2.0 | Apache-2.0 (verify per release) | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | ⚠️ verify weights |
| **PP-YOLOE** (PaddleDetection) | Apache-2.0 | Apache-2.0 | ✅ | ✅ | ✅ | ✅ | notice file | no | ✅ | ✅ | **✅** |
| Ultralytics YOLOv5/v8/v11 | **AGPL-3.0** | **AGPL-3.0** | ⛔ | ⛔ | ⛔ | (paid licence exists) | — | **yes, to avoid AGPL** | ⛔ | ⛔ | **⛔** |
| YOLOv7 | **GPL-3.0** | GPL-3.0 | ⚠️ | ⛔ derivative risk | ⚠️ | ✅ | required | no | ⚠️ | ⚠️ | **⛔ (copyleft)** |
| YOLO-NAS (super-gradients) | Apache-2.0 (code) | **non-commercial** | ⛔ (weights) | ⛔ | ⛔ | ⛔ | — | yes | ⛔ | ⛔ | **⛔ (weights)** |

Notes:
- Apache/MIT "attribution" = keep the licence/NOTICE file in the distribution; it does
  **not** require public-facing credit. We satisfy it by shipping the NOTICE and listing
  the model here and in `LICENCES.md`.
- **Ultralytics offers a paid Enterprise licence** that removes AGPL. We do **not** take
  it: a permissive alternative (YOLOX/RT-DETR) gives equivalent capability with no
  per-product licence cost or ongoing commercial dependency. Policy: prefer permissive
  over paid-to-escape-copyleft.

---

## 2. Segmentation models

| Model | Code | Weights | Ship? | Note |
|---|---|---|:--:|---|
| **SAM 2** | Apache-2.0 | Apache-2.0 | **✅** | offline/annotation use |
| **MobileSAM** | Apache-2.0 | Apache-2.0 | **✅** | light seg |
| SAM (v1) | Apache-2.0 | Apache-2.0 | ✅ | heavier |
| RTMDet-Ins / YOLOX-Seg | Apache-2.0 | Apache-2.0 | **✅** | inline instance seg if needed |
| **FastSAM** | **AGPL-3.0** | ⛔ | **⛔** | Ultralytics-based |
| YOLOv8-seg | **AGPL-3.0** | ⛔ | **⛔** | — |

---

## 3. Trackers

| Tracker | Code | Extra weights | Ship? | Note |
|---|---|---|:--:|---|
| **ByteTrack** | **MIT** | none | **✅** | appearance-free → no ReID licence exposure |
| **OC-SORT** | **MIT** | none | **✅** | appearance-free |
| BoT-SORT | MIT (code) | optional ReID | ⚠️ | ship only with a **permissive** ReID model; classic FastReID market1501 weights are research/attribution-encumbered — verify |
| SORT | **GPL-3.0** | none | **⛔** | copyleft; ByteTrack supersedes |
| DeepSORT | MIT (code) | ReID weights | ⚠️ | classic ReID weights often restricted — avoid unless cleared |

---

## 4. Open-vocabulary (development-only — never shipped to the mine)

Kept out of the production distribution entirely; used on the training workstation to
pre-label data. Their licences still matter (we run them), but SaaS/on-prem product
clauses do not apply because they are not in the shipped product.

| Model | Code | Weights | Dev use | Ship? |
|---|---|---|:--:|:--:|
| **Grounding DINO** | Apache-2.0 | Apache-2.0 | ✅ | (dev-only) |
| **OWLv2 / OWL-ViT** (HF) | Apache-2.0 | Apache-2.0 | ✅ | (dev-only) |
| YOLO-World (orig.) | GPL-3.0 | ⚠️ | ⚠️ | ⛔ |
| YOLOE | **AGPL-3.0** | ⛔ | ⛔ | ⛔ |

---

## 5. Depth models

| Model | Code | Weights | Ship? | Note |
|---|---|---|:--:|---|
| Depth-Anything-V2 **Small** | Apache-2.0 | **Apache-2.0** | ⚠️ | *Small* only; relative depth; non-safety |
| Depth-Anything-V2 Base/Large | Apache-2.0 | **CC-BY-NC-4.0** | **⛔** | non-commercial weights |
| ZoeDepth | MIT | MIT | ⚠️ | evaluate only if needed |

Default is **no learned depth** — calibrated homography for ground distance
(`VISION_ARCHITECTURE.md` §7, `VISION_GPS_FUSION.md`). If a relative cue is ever added,
only the Apache-weighted Depth-Anything-V2 **Small** qualifies, and never as a
safety-decision basis.

---

## 6. Runtime & libraries

| Component | Licence | Ship? | Note |
|---|---|:--:|---|
| PyTorch | BSD-3 | ✅ (training) | |
| ONNX Runtime | MIT | **✅** | default inference backend |
| OpenVINO | Apache-2.0 | ✅ | Intel edge |
| **TensorRT** | **NVIDIA proprietary EULA** | ⚠️ | runtime we deploy *on* NVIDIA HW; redistribution restricted — do not vendor the SDK; bundle per EULA on the edge image only |
| OpenCV | Apache-2.0 | ✅ | ⚠️ avoid GPL ffmpeg builds; use LGPL/BSD codec set |
| `supervision` (Roboflow) | MIT | ✅ | |
| MMDetection / Detectron2 | Apache-2.0 | ✅ (training) | |
| ffmpeg / codecs | LGPL vs GPL varies | ⚠️ | **use LGPL builds**; a GPL ffmpeg build in the shipped image is a copyleft breach |

---

## 7. Datasets (training-data provenance also gates commercial use)

A model fine-tuned on a restrictively-licensed dataset can inherit that restriction.
Audited in `MINING_VISION_DATASET_STRATEGY.md`; summary:

| Dataset | Licence | Commercial-train? | Note |
|---|---|:--:|---|
| **COCO** (images) | images = original Flickr terms; annotations CC-BY-4.0 | ⚠️ | pretrained backbones fine; using COCO *images* commercially needs care — we rely on Apache **weights**, not redistributing COCO images |
| **Open Images** | annotations CC-BY-4.0; images various CC | ⚠️ | usable with attribution; per-image licence varies |
| **Roboflow Universe** (mining sets) | **per-dataset — varies wildly (CC-BY, CC0, non-commercial, unknown)** | ⚠️ per set | **each set audited individually before use**; many are non-commercial — reject those |
| **Synthetic** (our engine) | our own | ✅ | we own it |
| **eigenstate/RAN Mines site data** | our own, with mine as data controller | ✅ | the authoritative set; consent/retention per `CLAUDE.md` §4 |

**Rule:** we do not redistribute third-party dataset *images*; we ship *weights* whose
code+weights licence is permissive, and we own the mining/site data we fine-tune on.
Any public mining dataset is licence-audited per set, and non-commercial sets are
rejected outright.

---

## 8. Audit process (ongoing)

1. Before any model enters the registry at `experimental`, its licence line is added
   here (code licence, weights licence, dataset licence, the eight answers).
2. A registry constraint / review checklist blocks promotion to `production` for any row
   not marked ✅ ship on **all three** of code, weights, and training data.
3. On every model version bump, re-audit — licences change per release.
4. `LICENCES.md` gets a one-line pointer per shipped vision component at build time (not
   before — nothing is shipped yet).
5. Transitive scan: the edge image's full dependency tree is licence-scanned (e.g. a
   permissive SBOM tool) before any release; a GPL/AGPL transitive dependency fails the
   build.

**Current position:** nothing is shipped. The permissive default stack
(`VISION_MODEL_CATALOG.md` §7) is pre-cleared by this audit; Ultralytics/AGPL,
YOLO-NAS weights, non-commercial depth weights, and GPL trackers/codecs are pre-excluded.
