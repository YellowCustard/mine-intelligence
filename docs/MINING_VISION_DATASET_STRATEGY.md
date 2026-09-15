# Mine Monitor — Mining Vision Dataset Strategy

**Status: strategy for review. No dataset has been collected, labelled, or trained on.**

Covers Phase 7 (dataset strategy) and Phase 8 (data collection pipeline). A general
pretrained model does **not** understand a Zimbabwean gold mine — dust, night, sunrise
glare, ochre haul roads, occlusion, unusual equipment poses. The dataset is what closes
that gap, and it is ultimately the most valuable IP in the vision stack.

## Principle

> Pretrained perception is the *starting point*, not the product. The model is only
> trusted once it is measured against **real mine conditions**. "Looks good on a demo
> clip" is not evidence (`VISION_TRAINING.md`).

Four dataset tiers, in increasing value and specificity:

```
Generic pretrained data → Public mining data → Synthetic data → eigenstate/RAN Mines site data
   (perception prior)      (licence-audited)    (rare/hard cases)   (the authoritative set)
```

---

## 1. Generic pretrained data (the prior)

- **What:** COCO / Open Images–pretrained backbones ship with YOLOX, RT-DETR, RF-DETR,
  RTMDet (Apache-2.0 weights, `VISION_MODEL_LICENSES.md`).
- **Role:** initialise perception. `person`, `truck`, `car`, `bus` transfer partially;
  `excavator`/`loader`/`dozer`/`grader`/`drill_rig`/`water_truck` do **not** exist in
  COCO and must be learned by fine-tuning.
- **Licence:** we rely on the **Apache weights**, not on redistributing COCO images.
- **Never** ship a model as "mining-ready" on COCO alone — it will confuse a haul truck
  with a lorry and miss every excavator.

## 2. Public mining datasets (use where licences permit — audit each)

- **What:** mining/construction-equipment sets on Roboflow Universe, Open Images subsets
  (truck/bus/person), academic construction-safety and PPE sets.
- **Role:** bulk up the equipment classes and PPE before site data exists.
- **Hard rule (from `VISION_MODEL_LICENSES.md` §7):** **each set is licence-audited
  individually.** Roboflow Universe licences vary from CC0 to non-commercial to unknown.
  **Reject any non-commercial or unknown-licence set** — a commercially-restricted
  dataset can taint the fine-tuned weights.
- Track every included set's name, licence, and version in the dataset manifest.

## 3. Synthetic data (for what reality won't hand us on schedule)

Cheap, perfectly-labelled, and controllable — ideal for the long tail.

- **Generate:** rare/dangerous scenarios (person in a restricted zone, unsafe proximity),
  under-represented equipment classes (drill rig, grader), and hard conditions — camera
  angle/height variation, lighting (day/night/sunrise/sunset), occlusion, **dust**, rain,
  fog, motion blur.
- **How:** game-engine / rendering pipelines (Blender, Unreal/AirSim-style), or
  augmentation of real frames. Domain randomisation to avoid overfitting the synthetic
  look; a synthetic-to-real gap is expected and measured, not assumed away.
- **Role:** bootstrap classes and safety scenarios we cannot ethically stage on a live
  mine. Synthetic never *replaces* the site validation set — it augments training only.
- **Licence:** we own it.

## 4. eigenstate / RAN Mines site data (the authoritative set)

This is the dataset that makes the product real, and the validation set that decides
whether a model may go to `production`.

- **Collect at RAN Mines (Bindura):** across **day · night · sunrise · sunset · dust ·
  rain · fog**, multiple **camera angles and distances**, **occlusion**, multiple
  equipment types, and both **busy and empty** scenes.
- **Role:** final fine-tuning (site adaptation) *and* the held-out validation/test set
  that all performance numbers are reported on (`VISION_ARCHITECTURE.md` §12).
- **Data protection (`CLAUDE.md` §4 — non-negotiable):**
  - The **mine is the data controller**; eigenstate is the processor. Site imagery is
    processed under that basis, scoped per site, exportable and deletable on request.
  - Frames may contain identifiable people. **No biometric templates, ever.** Labels are
    `person`, never identity. Where a person's face is incidental, blur/crop for any
    frame that leaves the edge; only structured events + necessary evidence clips cross,
    under retention policy.
  - Site data governance (consent posture, retention class, POTRAZ position) follows the
    same track already established for operational data; a stricter legal answer must
    cost configuration, not architecture.

---

## 5. Data collection & annotation pipeline (Phase 8)

Format-neutral and platform-neutral by design — we are not locked to one annotation
vendor.

```
 video (edge ring buffer / recorded)
   │  extract
   ▼
 frames  ── sample: adaptive rate + hard-example mining (keep frames the model is unsure on)
   │
   ▼
 pre-label  ── Grounding DINO / OWLv2 (Apache, dev-only) proposes boxes  →  HUMAN REVIEW
   │           (auto-labels are never trusted unreviewed)
   ▼
 annotations  ── stored in a neutral canonical form; export to COCO JSON *or* YOLO txt
   │
   ▼
 dataset version  ── immutable, hashed, manifested (see §6); train/val/test split fixed
   │
   ▼
 training → validation → model version → deployment  (VISION_TRAINING.md / _DEPLOYMENT.md)
```

Requirements:
- **Annotation formats:** support **COCO** (JSON) and **YOLO** (txt) with lossless
  conversion between them. The canonical internal store is format-agnostic; exporters
  produce either. No dependency on a single annotation SaaS — self-hostable tooling
  (e.g. CVAT/Label Studio, both permissively licensed) is preferred so labels stay on
  infrastructure we control.
- **Sampling:** don't label every frame — adaptive sampling plus **active learning /
  hard-example mining** (prioritise frames where the current model is low-confidence or
  where tracking id-switched), so annotation effort buys the most accuracy.
- **Class balance:** track per-class counts; synthetic/public data fills gaps for rare
  classes so the site set isn't skewed to haul trucks.
- **Splits are fixed and leakage-free:** the same *scene/day/camera* never spans train
  and test (temporal/spatial leakage inflates numbers). The validation and test sets are
  **site data**, held out.

---

## 6. Dataset versioning & manifest

Datasets are versioned like model weights — immutable, reproducible, and referenced by
the model registry (`VISION_ARCHITECTURE.md` §6), so any prediction traces to the exact
data that trained its model.

A dataset version manifest records:
`dataset_id · version · created_at · image_count · per_class_counts · source_breakdown
(generic/public/synthetic/site %) · included_public_sets [{name, licence, version}] ·
split (train/val/test, with the leakage rule) · annotation_format · content_hash ·
licence_status (must be all-clear) · storage_ref (MinIO)`

Rules:
- **Never mutate a released dataset version;** corrections create a new version.
- A dataset version with any non-commercial/unknown-licence included set is blocked from
  producing a `production` model (enforced with the licence audit).
- Site data retention/erasure requests are honoured at the dataset level (deletable per
  site), consistent with the platform's per-site export/delete posture.

---

## 7. Roadmap (data, not code — gated by review)

1. Assemble the **licence-audited public + synthetic** bootstrap set for the seven MVP+
   equipment/person classes → **Mining Equipment Detector v1** training input
   (`VISION_TRAINING.md` Phase 15).
2. Stand up the self-hosted annotation + versioning pipeline (CVAT/Label Studio + a
   manifest store) — no live camera required.
3. On camera install at RAN Mines, begin **site data** collection across the condition
   matrix; this becomes the fine-tuning + validation authority.
4. Every model reports metrics **on the site validation set**, recorded in the registry.

**STOP:** this is strategy. No data is collected or labelled until camera deployment and
review; the pipeline is designed here so it exists before the footage does.
