# Mine Monitor — Vision Training Strategy

**Status: strategy for review. No training has been run and no dependency added.**

Covers Phase 15 (first training target) and Phase 16 (mining-specific fine-tuning). The
rule that governs everything here: **transfer learning from permissive pretrained
weights — never train from scratch** (no demonstrated reason, and no data to justify it),
and **never accept "looks good" as validation.**

## 1. First training target — Mining Equipment Detector v1

Only *after* the pretrained MVP pipeline works end-to-end (`VISION_ARCHITECTURE.md` §10)
do we train the first mining model.

- **Task:** object detection.
- **Base:** a permissive pretrained detector — **YOLOX** (CPU/edge) and/or **RT-DETRv2**
  (GPU), both Apache-2.0 code+weights (`VISION_MODEL_LICENSES.md`).
- **Classes (v1):** `haul_truck · excavator · loader · dozer · grader · light_vehicle ·
  person`. (PPE and infrastructure are later, separate capabilities.)
- **Method:** transfer learning — freeze/adapt the backbone, retrain the head on the
  mining classes, then unfreeze for full fine-tuning. Never from scratch.
- **Registry:** `mine-equipment-detector`, `version: 0.1.0`, `status: experimental`,
  `base: yolox-s` (or `rt-detrv2-r50`), `dataset: mine-vision-v1` — recorded in the
  model registry with its dataset version and licence line before any inference is used.

## 2. The fine-tuning ladder (Phase 16)

Adaptation is staged from general to site-specific; each stage is a distinct, versioned
model, evaluated on the **site validation set**.

```
 base pretrained weights            (Apache; COCO/OpenImages prior)
        │  fine-tune
        ▼
 generic mining dataset             (licence-audited public + synthetic)
        │  → mine-equipment-detector 0.1.x  (status: validation)
        ▼
 Mine Monitor validation set        (held-out RAN Mines site data — the gate)
        │  → promote to staging only if metrics pass §4
        ▼
 site-specific dataset              (RAN Mines, condition matrix)
        │  fine-tune (site adaptation)
        ▼
 site-adapted model                 (per-site version; e.g. ran-mines/1.0.0, production)
```

- Site adaptation may be **per-site** — a Bindura-adapted model is a distinct registry
  version, never overwriting the generic one. Multi-site means multiple production
  versions, selected per `site_id`.
- Each rung is reproducible: `(base weights ref) + (dataset version) + (training config)`
  is recorded, so any model can be rebuilt and any prediction traced.

## 3. Repeatable training pipeline

Config-driven, deterministic where possible, and self-hostable (no mandatory training
SaaS). Frameworks are permissive: PyTorch (BSD), and the detector's own Apache-2.0
training code (YOLOX repo, MMDetection for RTMDet/DETR family).

```
 dataset version (immutable, manifested)
   → training run (seeded config: base, lr schedule, augmentation, epochs, splits)
   → checkpoints
   → evaluation on the held-out SITE validation/test set
   → metrics recorded → model registry (experimental → validation → staging → production)
   → export (ONNX; TensorRT/OpenVINO per target) → VISION_DEPLOYMENT.md
```

Every run records: dataset version, base weights, full hyper-config, git SHA of the
training code, hardware, duration, and the resulting metrics — attached to the registry
version. A model is **not** promoted on a training-loss curve; only §4 metrics on
held-out **site** data promote it.

## 4. Validation — measured, never "looks good"

Report on the held-out **site** validation and test sets (leakage-free splits per
`MINING_VISION_DATASET_STRATEGY.md` §5). Track and record all of:

**Accuracy**
- precision, recall
- mAP@0.5 and mAP@[.5:.95]
- **per-class** performance (a high mean can hide that `grader` recall is 0.2)
- false positives / false negatives, with confusion between look-alike classes
  (haul_truck vs water_truck vs service_truck; loader vs dozer)
- performance **sliced by condition**: day/night/dust/rain/glare — a model that is 0.9
  in daylight and 0.4 in dust is not production-ready for a mine

**Operational (the metrics that actually matter to the mine)**
- do the *operational events* (zone entry, queueing, proximity) fire correctly? A
  detector with good mAP can still produce bad operational events if tracking or timing
  is weak. Evaluate the **end-to-end** pipeline against annotated event ground truth,
  not just the detector in isolation.
- track-id stability on the validation sequences (id switches per track).

**Cost**
- inference latency, FPS, GPU memory / CPU usage per target profile
  (`VISION_ARCHITECTURE.md` §12).

**Promotion gate:** a model reaches `production` only when per-class accuracy clears the
agreed threshold **on site data across the condition matrix**, the end-to-end
operational events are correct, cost fits the target profile, and the licence line is
green. All figures live in the registry — no COCO numbers stand in for mining numbers.

## 5. Continuous improvement loop

```
 production model → runs on site → low-confidence / disagreement frames flagged
   → active-learning queue → human review/label → dataset vN+1
   → retrain → validate on site set → new version → promote (or roll back)
```

- **GNSS/vision disagreements** (fusion says "probably not the same asset" when the
  operational rule assumed it was) are high-value hard examples — they feed the queue.
- Rollback is a registry status change to the prior production version — **never** a
  weight overwrite (`VISION_ARCHITECTURE.md` §6).
- The loop is the mechanism by which perception improves **without a platform rewrite** —
  the operational layer and contracts are untouched by a model version bump.

## 6. Constraints (restated)

- No training from scratch without a demonstrated, documented reason.
- No non-commercial or AGPL base weights (`VISION_MODEL_LICENSES.md`).
- No fabricated metrics or capabilities: if a class has too little data to validate, it
  ships `Unknown`-capable or not at all — never a guessed detector paraded as reliable.
- Training data provenance is audited; a model inherits its dataset's licence status.

**STOP:** training begins only after the MVP pipeline and dataset bootstrap exist and
these documents are reviewed.
