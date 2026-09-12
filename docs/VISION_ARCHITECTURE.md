# Mine Monitor — Vision Architecture

**Status: architecture foundation for review. No vision model code, dependency, or
training has been introduced. This document and its siblings are the deliverable of
the Computer Vision *foundation* phase; full implementation is gated on their review.**

Sibling documents:
`VISION_MODEL_CATALOG.md` · `VISION_MODEL_LICENSES.md` ·
`MINING_VISION_DATASET_STRATEGY.md` · `VISION_TRAINING.md` ·
`VISION_DEPLOYMENT.md` · `VISION_EVENT_CONTRACTS.md` · `VISION_GPS_FUSION.md`.

This document is the master. It defines the pipeline, the hard boundary between
perception and operational meaning, the object and operational ontologies, the model
adapter and registry abstractions, the edge service boundary, temporal reasoning, the
safety line, the MVP plan, hardware and performance targets, and the testing strategy.
The other documents drill into model selection, licensing, data, training, deployment,
the event contracts, and GPS/vision fusion.

---

## 0. How this fits Mine Monitor (what already exists — do not duplicate)

Vision is **not a new platform**. It is a new *source* that speaks the event contract
the platform was already built to receive. `docs/PLATFORM_EVOLUTION_ARCHITECTURE.md`
§2.1 already ruled vision **the one capability that justifies a separate edge
service** (GPU/video runtime, must not centralise raw video). This document builds on
that ruling; it does not relitigate it.

The following are Mine Monitor concepts that vision **reuses, never re-implements**:

| Concept | Where it lives today | How vision uses it |
|---|---|---|
| `site_id` multi-tenancy | every operational table | every camera, observation and event is site-scoped |
| Assets & `asset_class` | `assets` table (`generic`, `haul_truck`, …) | fusion resolves a detection to an existing `asset_id`; vision never invents assets |
| Zones & `kind` | `zones` (`loading`/`unloading`/`restricted`/`speed_limited`/`generic`) | camera image-zones map to the *same* operational zones; operational meaning reuses zone rules |
| `event.v1` | `contracts/event.v1.json`, `contracts/event.py` | vision operational events are **plain `event.v1`** with `source: "vision:<cam>"` (the schema already lists `proximity` and `evidence.clip_uri`) |
| Unified alarm queue, ack workflow | `events/`, dashboard | vision alarms land in the *same* queue; the control room groups by severity, not source |
| Contract registry, event bus, metrics | `platform/contracts.py`, `platform/bus.py`, `platform/metrics.py` | new `vision.*.v1` contracts register here; the core publishes them on the bus |
| RBAC | `auth/deps.py` (`viewer<supervisor<admin`, `device`) | camera/vision-node accounts are **`device`-role** principals; no new auth |
| Audit log | `audit_log` | camera config and model-version changes are audited like zone/rule changes |
| Evidence object storage | MinIO (S3) | clips/frames stay on the edge; MinIO holds only referenced evidence, per retention |
| Retention (per data class) | `retention.py` | a new `vision_frames`/`vision_clips` class extends the existing job |
| `/api/v1` | `api/main.py` dual-mount | all vision read/config endpoints mount under `/api/v1` |

**The one-line rule:** the neural network answers *"what is in the frame?"*; Mine
Monitor's operational layer answers *"what does that mean on this site?"* Everything
below enforces that split.

---

## 1. The pipeline

```
 Camera (RTSP/ONVIF, or recorded file for dev)
   │                                            ── stays on the edge node ──
   ▼
 Video ingestion            decode, reconnect, backpressure
   │
   ▼
 Frame sampling             adaptive rate (e.g. 2–5 fps), drop-old-not-recent
   │
   ▼
 Perception (models)        detection · (optional) segmentation · classification
   │                        → raw boxes/masks/labels + confidence, per frame
   ▼
 Tracking                   ByteTrack/OC-SORT → stable track_id across frames
   │
   ▼
 Spatial reasoning          image-zone test · homography → ground point ·
   │                        pairwise distance · camera→site frame
   ▼
 Temporal reasoning         per-track state over a time window; debounce/hysteresis
   │                        (mirrors the GNSS zone-debounce discipline)
   ▼
 Mining activity engine     operational-ontology state machine (rules, not the NN)
   │                        ── boundary: everything above is perception ──
   ▼
 Structured vision observation   vision.observation.v1  (raw, immutable, provenance)
   │                             vision.operational_event.v1  (interpreted, evidenced)
   ▼   published on MQTT topic  mm/<site>/vision/<cam>   (thin: JSON, optional clip ref)
 ═══════════════════════════════ edge ↑  │  ↓ core ═══════════════════════════════
   ▼
 Core ingestor              validate against the registry · store · fuse with GNSS
   │
   ▼
 event.v1  →  unified alarm queue · analytics · reports · Command Centre dashboard
```

Only the two structured JSON contracts (and, where an event warrants it, a clip
*reference*) cross the edge→core boundary. **Raw video and frames never leave the edge
node.** This is both a bandwidth constraint (thin, expensive mine link) and a
data-protection posture (frames may contain identifiable people; see §8 and
`VISION_MODEL_LICENSES.md` is *not* where that lives — data protection is in
`CLAUDE.md` §4 and honoured here).

---

## 2. The perception ↔ operational boundary (mandatory)

This separation is the core architectural decision. It is what lets a better model
drop in later without touching Mine Monitor, and what keeps inferred guesses from being
recorded as operational facts.

### 2.1 What perception may assert (edge, `vision.observation.v1`)

Perception describes pixels and their motion. Nothing else.

- `haul_truck detected`, `excavator detected`, `person detected`
- bounding box / mask, in image coordinates
- class confidence (0–1)
- `track_id` (temporal identity *within one camera*, from the tracker — not an asset id)
- observed motion (image-space velocity, direction)
- optional: PPE class present/absent on a person crop (a separate classifier)

Provenance on every object: `observed` (a direct detection) — perception never emits
`inferred`/`correlated`.

### 2.2 What the operational layer may assert (`vision.operational_event.v1` → `event.v1`)

Operational meaning is produced by **rules over perception + tracking + zones +
time + GNSS + existing equipment state** — never by the network.

- `truck entered loading zone` (track enters the image-zone mapped to a `loading` zone)
- `truck is queueing` (in-zone + stationary + waiting behind another truck, over time)
- `truck appears loaded` (post-loading-interaction departure — *appears*, labelled inferred)
- `truck arrived at dump` / `truck completed dumping`
- `unsafe proximity detected` (person↔equipment ground distance below threshold, sustained)
- `loading cycle started` / `loading cycle completed`

Provenance on every operational event: `inferred` (rule over observations),
`correlated` (tied to a GNSS asset — see `VISION_GPS_FUSION.md`), or `estimated`
(a measured-adjacent value like distance from homography), each with the **evidence**
that produced it (the contributing track ids, frames, zone, and time window).

### 2.3 Why the boundary is non-negotiable

- **Trust.** A mine manager must always be able to see whether "truck loaded" was
  weighed (measured), computed, or *guessed from a camera* (inferred). Collapsing the
  boundary destroys that distinction — the same failure the platform already guards
  against for fuel and maintenance.
- **Replaceability.** Operational rules are stable; models churn. If the model emitted
  "loading complete" directly, every model swap would be an operational-logic change.
- **Retro-computability.** Like haul cycles, operational events must be recomputable
  from stored `vision.observation.v1` when a rule improves. If the NN emitted the
  conclusion, there is nothing to recompute from.
- **Safety.** §8: an operational recommendation must be explainable to a human. A rule
  with cited evidence is; a raw network output is not.

---

## 3. Mining object ontology (perception classes)

The **visual** ontology — what a model is asked to *see*. Extensible; versioned with
the model that detects it (a model card in the registry declares which classes it
supports, §6). Grouped so a model can implement a subset.

**Heavy equipment**
`haul_truck` · `excavator` · `loader` · `dozer` · `grader` · `water_truck` ·
`service_truck` · `drill_rig` · `light_vehicle` · `bus`

**People**
`person` · `operator` · `pedestrian`
*Identity is out of scope for perception.* Vision emits `person`, not "who". Any future
person-identity capability is a separate, consented, appropriately-architected system
and would follow `CLAUDE.md` §4 (no biometric templates in this database, ever).

**Safety equipment (a separate PPE capability — a classifier on person crops)**
`hard_hat` · `hi_vis_vest` · `safety_boot` · `safety_glasses`
Treated as present/absent attributes of a `person` detection, not top-level objects.

**Mining infrastructure (later; largely static, often better as configured zones)**
`crusher` · `conveyor` · `stockpile` · `dump_area` · `loading_face` · `haul_road` ·
`workshop` · `fuel_station` · `weighbridge`
Static infrastructure is usually more reliably represented as a **configured zone**
than as a per-frame detection; detect only what genuinely moves or changes.

The ontology is published as data (`contracts/vision.ontology.v1.json` when
implemented), so classes can be added without a code change, exactly as zone rules are
data today.

---

## 4. Mining operational ontology (states — produced by rules, not the NN)

Per the boundary, these states are **derived**, each retaining its evidence. They are a
per-track state machine that consumes perception + zones + time + GNSS + the existing
`asset_zone_state`/equipment-state the platform already computes for GNSS assets.

**Truck**
`detected → tracked → approaching_loading_area → queueing → positioning → loading →
loaded → departing → hauling → approaching_dump → dumping → returning → stopped`

**Excavator / loader**
`detected → idle → positioning → loading_truck → digging → swinging → dumping`

**Person**
`detected → inside_zone → near_equipment → near_vehicle → restricted_area`

Design notes:
- These reuse and cross-check the **GNSS haul-cycle state machine** already in
  `cycles/`. Where a truck is also a GNSS asset, vision and GNSS agree → high
  confidence; they disagree → a data-quality signal, not a silent overwrite.
- Transitions require **temporal evidence** (§7): N consecutive frames / a minimum
  dwell, with hysteresis — the same debounce discipline that keeps GNSS geofencing from
  emitting event storms.
- A state is only promoted to an `event.v1` alarm when it matters to a human (e.g.
  `restricted_area`, `unsafe proximity`). Routine transitions feed analytics, not the
  alarm queue — again mirroring "emit `event.v1` only when a *rule* is breached".

---

## 5. Model adapter interface (vendor isolation)

Mine Monitor must be **model-agnostic**. No part of the platform, and no operational
rule, may import a model vendor's SDK or depend on its output shape. All models sit
behind one narrow interface on the edge node.

```
VisionModel (abstract)
  .info() -> ModelInfo            # id, version, task, classes, input size, backend
  .detect(frame) -> [Detection]   # class, confidence, bbox(xyxy, normalised)
  .segment(frame) -> [Mask]       # optional; NotImplemented if unsupported
  .classify(crop) -> [Label]      # optional; e.g. PPE on a person crop

Detection  = {class, confidence, bbox, provenance="observed"}
Tracker (separate layer, NOT part of VisionModel)
  .update([Detection]) -> [Track] # adds stable track_id; ByteTrack/OC-SORT/…
```

Concrete adapters (all behind the same interface; see `VISION_MODEL_CATALOG.md`):
`RTDetrAdapter`, `YoloxAdapter`, `Sam2Adapter` (segmentation/annotation), a future
`RfDetrAdapter`, etc. **Tracking is a distinct layer**, never folded into the detector —
so the tracker and detector version independently, and a detector swap keeps track ids.

Two hard rules the adapter boundary enforces:
1. Every adapter normalises to the *same* `Detection`/`Mask`/`Label` shapes. The
   perception→operational code never learns which model produced them.
2. No adapter for an **AGPL-licensed** package may be built for the shipped product.
   The interface is permissive-only; the catalog and license docs decide what may sit
   behind it. This is why the default detector is **not** an Ultralytics YOLO
   (`CLAUDE.md`: Ultralytics is AGPL-3.0 and must not enter this codebase).

---

## 6. Model registry (predictions stay distinguishable and reversible)

Every model that produces observations is registered so a prediction is always
traceable to the exact weights and data that made it, and so production weights are
never silently overwritten. The registry is **in-core** (predictions must be
distinguishable from measured facts even when the edge node is gone), mirroring
`PLATFORM_EVOLUTION_ARCHITECTURE.md` §4.

A model record carries:

`model_id · name · version · task · architecture · weights_ref (MinIO) · license ·
training_dataset · dataset_version · training_date · metrics · deployment_target ·
model_status`

`model_status ∈ { experimental · validation · staging · production · retired }`

Rules:
- **Never overwrite production weights.** A new model is a new `version`; promotion
  moves status, it does not mutate a row's weights.
- Every `vision.observation.v1` embeds `model.id` + `model.version` (provenance). An
  observation can always be traced to its exact model and dataset.
- Rollback is a status change back to a prior production version — never a delete.
- The registry lists licence per model so an AGPL/non-commercial model can never reach
  `production` (enforced in review + a registry constraint; see
  `VISION_MODEL_LICENSES.md`).

Registry CRUD is admin-only and audited, exactly like device provisioning.

---

## 7. Temporal reasoning (do not decide from one frame)

Single-frame decisions are forbidden for anything operational — a detection blinks, a
box jitters, a person is briefly occluded. Temporal reasoning is the analogue of GNSS
debounce/hysteresis and is mandatory.

```
f1: truck detected (track 37)
f2: truck detected (track 37)          ← stable id ⇒ same object
f3: track 37 enters loading image-zone ← candidate "entered", not yet confirmed
f4: excavator (track 12) near track 37
f5: track 37 stationary in zone
f6: track 37 leaves loading zone
⇒ inference: a loading interaction likely occurred for track 37
   evidence: {tracks:[37,12], zone: r-load-1, window: f3..f6, frames:[…]}
   provenance: inferred; confidence: from dwell + proximity + class conf
```

Requirements:
- **Windowed, per-track state**, not per-frame classification.
- **Confirm with N observations + minimum dwell; release with hysteresis** — configurable
  per camera/zone, defaults echoing the zone engine (N=2 in, 15 m / buffer out).
- **Evidence is retained** on every inference: contributing track ids, the frame ids /
  time window, the zone, and the per-signal contributions to confidence. This is what
  makes an operational event explainable and recomputable.
- Late/out-of-order observations are tolerated (the mine link is flaky) — the temporal
  engine is idempotent and re-derivable from stored observations, like haul cycles.

---

## 8. Safety and data protection (the line that does not move)

- **Vision never actuates plant.** It may detect, classify, alert, recommend, and
  provide evidence. Creating incidents, alarms, dispatch information, and analytics is
  Mine Monitor's job — and every one is **advisory** (`event.v1.advisory === true`,
  already an invariant). No automatic machine control, ever. A requirement that crosses
  this line is escalated, not implemented (`CLAUDE.md` §15).
- **No biometric templates in this database, ever** (`CLAUDE.md` §4). Perception emits
  `person`, not identity. Face images/templates, if a consented gate system ever
  exists, stay in that vendor appliance; Mine Monitor ingests only an access *event*.
- **Raw video stays on the edge.** Only structured events and referenced clips cross;
  clip retention is configurable per data class and deletable per site.
- **Every inferred value is labelled and evidenced.** `observed/inferred/correlated/
  estimated/unknown` + confidence travel with the data; the dashboard must render the
  label, never present a guess as a measurement.

---

## 9. Vision service boundary

Computer vision is a **separate edge process/service** from the Mine Monitor core:

```
Camera → Vision Edge Service (own process; CPU or GPU) → vision.*.v1 (MQTT) → Core
```

- The core does not know, and must not care, whether an observation came from RT-DETR,
  YOLOX, SAM, or a future model. It validates `vision.*.v1` against the registry and
  proceeds. This is the long-term maintainability guarantee.
- The edge node is **independently deployable and independently failing**: if it is
  offline, the core operates unaffected (GNSS telemetry, geofencing, alarms, analytics
  all continue). Vision degrades gracefully to "no vision", never to "core down".
- The edge node authenticates to the broker as a **`device`-role principal** with a
  per-node secret and an ACL confined to `mm/<site>/vision/<cam>` — reusing the exact
  device-credential model already built for trackers. No new auth surface.

---

## 10. First MVP (Phase 14) — the smallest end-to-end slice

Deliberately minimal, and buildable **from a recorded video file — no live camera, no
GPU, no site hardware required**:

```
recorded video → permissively-licensed pretrained detector (haul_truck only)
  → ByteTrack (stable track_id) → one configured image-zone
  → vision.observation.v1 (stored) → one temporal rule ("entered loading zone")
  → vision.operational_event.v1 → event.v1 (source: "vision:cam-sample")
  → Command Centre alarm queue
```

Acceptance for the MVP:
- A truck crossing the configured zone in the sample clip raises **exactly one**
  `event.v1` (debounced), visible and acknowledgeable in the existing dashboard.
- A boundary-hugging or briefly-occluded truck raises **none** (temporal hysteresis
  holds) — the same acceptance bar M3 set for GNSS geofencing.
- The observation is stored with model id/version provenance and is replayable.
- The core runs identically whether the edge slice is present or absent.

Explicitly **not** in the MVP: multi-class detection, segmentation, GPS fusion, PPE,
proximity, training. Each is a subsequent increment (see the sequence in
`PLATFORM_EVOLUTION_ARCHITECTURE.md` §7 — vision follows the fuel/weighbridge quick
wins) and each is its own additive, tested step.

---

## 11. Hardware requirements

Vision is the only capability that may need a GPU, so hardware is a real decision, not
an assumption. Three target profiles (detailed in `VISION_DEPLOYMENT.md`):

| Profile | Hardware | Realistic throughput | Use |
|---|---|---|---|
| **CPU-small** | mini-PC / existing site server, 4–8 cores, no GPU, OpenVINO/ONNX | 2–5 fps, small detector (YOLOX-S/-Nano class) | 1–2 cameras, quiet zones, the MVP |
| **GPU-balanced** | edge box w/ NVIDIA (e.g. Jetson Orin or a T4/RTX-class), TensorRT | 15–30 fps, RT-DETR-R50 class | several cameras, active zones |
| **GPU-accuracy** | server GPU (RTX A4000+/L4-class) | 30 fps+, larger detector + segmentation | busy pit, proximity safety |

Non-assumptions, stated as rules:
- **Do not assume a GPU exists.** The default profile is CPU-small; the *same*
  `vision.*.v1` interface works on every profile.
- Cameras are RTSP/ONVIF IP cameras on the mine LAN; the edge node sits **next to
  them**, not in the cloud.
- Storage: local disk for a short ring buffer of frames/clips on the edge; MinIO holds
  only referenced evidence. Size to retention policy, not "keep everything".
- Network to the core is thin — design for it (JSON events, optional clip refs), which
  the pipeline already does.

Exact model sizes, memory, and per-profile FPS are enumerated in `VISION_MODEL_CATALOG.md`
and `VISION_DEPLOYMENT.md`.

---

## 12. Performance targets

Targets are per profile; "looks good" is not an acceptance criterion (`VISION_TRAINING.md`
governs accuracy). Measured, not asserted:

| Metric | CPU-small | GPU-balanced | GPU-accuracy |
|---|---|---|---|
| Detection fps (per camera) | ≥ 2 | ≥ 15 | ≥ 30 |
| End-to-end frame→observation latency | ≤ 750 ms | ≤ 250 ms | ≤ 150 ms |
| Observation→`event.v1` (core) | ≤ 1 s | ≤ 1 s | ≤ 1 s |
| Track-id stability (MOTA-adjacent, sample set) | ≥ 0.6 | ≥ 0.7 | ≥ 0.75 |
| Detector mAP@0.5 on the mining validation set | recorded per model, not fabricated | | |
| Edge node sustained CPU / VRAM headroom | ≤ 80% | ≤ 80% | ≤ 80% |

Accuracy figures (mAP, precision/recall per class) are **measured against the mining
validation set and recorded in the model registry** — never carried over from a
model's published COCO numbers, which say nothing about mining conditions.

---

## 13. Testing strategy (Phase 20)

Tests mirror the platform's existing discipline (unit for correctness-critical logic,
integration over fixtures, explicit failure-mode tests). Vision-specific suites:

- **Model interface conformance.** Two different adapters, fed the same frame fixture,
  produce the *same normalised output shape*; downstream code is model-blind. A stub
  adapter is the default test fixture — **no model weights or GPU in CI**.
- **Tracking.** On a fixture sequence, objects retain stable `track_id`; an object
  leaving and re-entering behaves per policy; id switches under crossing are bounded.
- **Temporal reasoning.** An operational event is emitted **only** with adequate
  temporal evidence; a single-frame blip does not; hysteresis suppresses boundary
  flicker (the GNSS-geofence acceptance bar, applied to vision).
- **GPS fusion.** A correct association is accepted; a wrong one (too far, wrong time,
  wrong heading) is **rejected**, not silently claimed (`VISION_GPS_FUSION.md`).
- **Zone logic.** Image-zone ↔ operational-zone mapping is correct; homography maps a
  known image point to a known ground point within tolerance.
- **Contract/provenance.** Every emitted `vision.*.v1` validates against the registry
  and carries model id/version + provenance + evidence; the core rejects malformed or
  unversioned observations loudly.
- **Failure modes (explicit).** camera offline · corrupted/partial frame · model
  unavailable · GPU unavailable (fall back to CPU profile) · inference timeout · high
  CPU / dropped frames · core unreachable (edge buffers, backfills — no data loss,
  matching the M2 store-and-forward bar) · duplicate observation (idempotent) ·
  out-of-order observations (re-derivable).
- **Performance.** fps, latency, CPU/RAM/GPU/VRAM, event throughput — recorded per
  profile against the sample video, so regressions are visible.

CI stays weights-free and GPU-free (stub adapter + recorded frames). Real-model and
real-hardware benchmarking runs on the edge box during commissioning, recorded in the
registry — the same "validated on real hardware, not mocks" posture the deployment
docs already take for Postgres/Mosquitto.

---

## 14. What this foundation delivers, and the STOP

Delivered by this document set (the Phase "final deliverable"):
model comparison · recommended stack · license analysis · mining ontology ·
operational ontology · dataset strategy · training strategy · adapter architecture ·
vision event contract · GPS/vision fusion architecture · edge deployment architecture ·
first MVP plan · hardware requirements · performance targets · testing strategy.

**STOP.** No model dependency, weights, training run, or edge-service code has been
added. The engineering principle holds: Mine Monitor becomes **model-agnostic,
mining-aware, and evidence-driven** — the network provides perception, tracking
provides temporal identity, the spatial engine provides context, the operational
engine provides meaning, and Mine Monitor remains the authoritative operational record.
Future models improve perception without a platform rewrite. Build begins only on
review of these documents and after the fuel/weighbridge quick wins, per the ranked
sequence in `PLATFORM_EVOLUTION_ARCHITECTURE.md` §7.
