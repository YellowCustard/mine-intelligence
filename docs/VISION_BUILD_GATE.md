# Mine Monitor — Vision Build-Gate Design

**Status: design for the Phase 2 build gate. Review deliverable, not an implementation.**
No model, weights, edge-service code, training code, adapter implementation, or
infrastructure has been produced or is assumed to exist. **The hard STOP from
`VISION_ARCHITECTURE.md` §14 stands: build begins only after John/Theo review and approve
this design.** This document sharpens the existing `VISION_*` foundation into something
precise enough to gate a build against; it supersedes nothing and deletes nothing — it
adds the contracts, boundaries, failure modes, volume/transport policy, ontology, temporal
framework, NVR-adapter contract and a hardware-free MVP that a build gate needs.

Vendor note: the site cameras are **Dahua** (previously mis-recorded as "Alhua" in some
notes; corrected throughout the repo).

Companion references (unchanged): `VISION_ARCHITECTURE.md` (master), `VISION_EVENT_CONTRACTS.md`,
`VISION_DEPLOYMENT.md`, `VISION_GPS_FUSION.md`, `VISION_MODEL_CATALOG.md`,
`VISION_MODEL_LICENSES.md`, `MINING_VISION_DATASET_STRATEGY.md`, `VISION_TRAINING.md`,
`RAN_MINES_PROPOSAL_ALIGNMENT.md`.

---

## 1. One-page executive brief (for John / Theo)

**What this is.** A design for how Mine Monitor will *see* the mine. It has two paths. The
**near-term path ingests the AI reports the existing Dahua NVR already produces** (line-crossing,
intrusion, vehicle) and turns them into alarms in the one alarm queue you already use. The
**longer-term path** runs our own perception models on a small computer **next to the cameras**
(the "edge"), for use-cases the NVR cannot do.

**What this is not.** It is not a new platform, not facial recognition, and not a system that
controls any machine. It does not identify *who* a person is — it reports "a person," never a name.

**Why NVR-first.** The mine already owns ~118 Dahua cameras whose NVR **already runs AI and
already generates reports that currently go unused**. Ingesting those needs **no new cameras, no
GPU, no server room** — only the vendor API/credentials. It is the fastest, cheapest, lowest-risk
value: existing detections become alarms and analytics in days of integration, not a hardware
project.

**Why raw video stays at the edge.** The mine link is a single Starlink connection — thin and
expensive. We never stream video to the cloud. Only tiny structured events (and, rarely, a
reference to a short clip) cross the link. This is also a data-protection posture: frames can
contain identifiable people, so they stay on the mine's own network.

**Why advisory only.** Every output warns a person; nothing brakes a truck or stops a conveyor.
That line is the difference between a software product and a multi-year functional-safety
programme. A request to cross it is escalated, never implemented.

**Why no biometric templates.** Zimbabwe's data-protection regime (Cyber & Data Protection Act +
SI 155 of 2024) treats facial-recognition features as sensitive biometric data. We store **none**.
If a consented face terminal is ever used at a gate, its templates stay inside that vendor
appliance; we ingest only an access *event*.

**Deliverable with no new hardware:** (a) NVR AI-report ingestion → alarms + analytics; (b) a
fully testable perception pipeline demonstrated on **recorded video** (no camera, no GPU) — the MVP
in §9.

**Requires GPU + server-room readiness later:** first-party, real-time, multi-camera perception
(person counting, restricted-area, unsafe-proximity) at accuracy that the NVR cannot reach. This
depends on the flagged **server-room relocation** (a GPU box cannot live in the kitchen container).

**Decision requested of this gate:**
1. Approve the **NVR-first** sequence and the two-path architecture below.
2. Approve the **contracts, boundary, ontology and temporal framework** as the build spec.
3. Authorise the **hardware-free MVP** (§9) as the first build increment.
4. Note the **open questions** (§11) that only the site visit / vendor spec can close.

**Why there is still a STOP.** No model is chosen, no weights exist, no edge code is written.
Approving *this design* authorises the MVP slice against recorded video only. First-party
real-time perception, model selection, and any hardware purchase remain behind their own gates,
because the two things that decide them — the real Dahua API/field spec and per-camera inference
suitability at distance/low-light — are only knowable after the site visit.

---

## 2. Revised architecture overview

Vision is **a source, not a platform**. It speaks the event contracts the platform already
receives (`RAN_MINES_PROPOSAL_ALIGNMENT.md` §2). Nothing below re-implements zones, events, RBAC,
audit, retention, MQTT, or storage — it reuses them.

### 2.1 Components

```
                         ── mine LAN (edge) ──────────────────────┐
 Dahua cameras ──RTSP/ONVIF──► Vision Edge Service (own process)  │
      │                          decode → sample → detect →       │
      │                          track → spatial → temporal →     │
      │                          operational rules                │
      │                                    │ vision.observation.v1 │
      │                                    │ vision.operational_event.v1
      │                                    │ vision.health.v1 / camera_health.v1
      ▼                                    ▼  (MQTT: mm/<site>/vision/<cam>)
 Dahua NVR ──vendor API──► NVR Adapter ──► vision.vendor_event.v1
 (already runs AI)          (event ingest, NOT perception)         │
 ══════════════════════════════ edge ↑ │ ↓ core ══════════════════┘
                                        ▼
 Core: validate against registry → store observations → correlate with GNSS/zones →
       generate operational events (versioned rules) → promote to event.v1 →
       unified alarm queue · analytics · reports · SSE dashboard
```

- **Edge process.** A separate, independently-deployable, independently-failing process on the
  mine LAN. If it dies, GNSS telemetry, geofencing, alarms and analytics continue unaffected —
  vision degrades to "no vision," never "core down." It authenticates to MQTT as a **`device`-role
  principal** with an ACL confined to `mm/<site>/vision/<cam>` (the existing tracker credential
  model — no new auth surface).
- **NVR adapter path (near-term).** A thin ingest driver (the `nvr.py` / `dahua_nvr_sim.py` shape
  already scaffolded) that polls the Dahua NVR's AI-report API, normalises each vendor event to
  `vision.vendor_event.v1`, and lets the core promote it. **This is vendor event ingestion, not
  first-party perception** — its provenance class differs (§3.6). The NVR runs the inference; we do
  not.
- **First-party perception path (longer-term).** Our own models behind the `.detect()/.segment()/
  .classify()` adapter interface, producing `vision.observation.v1`. Model-agnostic, licence-gated
  (no AGPL — no Ultralytics YOLO — without explicit legal approval).
- **MQTT contract.** Both paths publish thin JSON on `mm/<site>/vision/<cam>`; raw frames never
  cross (§5).
- **Zone & temporal rule engine.** Camera image-zones map onto the **same operational zones** the
  GNSS geofence engine already uses. Operational meaning is produced by **versioned rules over
  perception + tracking + zones + time + GNSS** — never by the model (§3, §7).
- **GNSS / geofence correlation.** Where a detected vehicle is also a GNSS asset, fusion may
  associate them with a labelled confidence (`VISION_GPS_FUSION.md`); agreement raises confidence,
  disagreement is a data-quality signal, never a silent overwrite.
- **Observation storage.** Write-once `vision_observations` (high-volume, hypertable), site-scoped;
  raw frames not stored — only structured objects. Operational events stored in a derived,
  recomputable table (like haul cycles).
- **Operational event generation → `event.v1` promotion.** When an operational event warrants a
  human it becomes a plain `event.v1` (`source: "vision:<cam>"`) in the **same** alarm queue,
  grouped by severity, not source. Ack/resolve/dedupe/SSE all unchanged.
- **Dashboard presentation.** Vision alarms render exactly like GNSS/gate alarms, **with the
  provenance label always shown** — a camera guess is never presented as a measurement.
- **Evidence clips.** A promoted event may carry a *reference* to a short clip retained on the edge
  (or in MinIO per retention), never an inline stream (§4.6).
- **Health monitoring.** The edge emits `vision.health.v1` (node) and `vision.camera_health.v1`
  (per camera) so a stuck/blinded/offline camera is visible and distinguishable from "quiet."
- **Degraded-mode behaviour.** Camera offline, model unavailable, GPU absent (fall back to CPU
  profile), core unreachable (edge buffers and backfills) — each is an explicit, tested mode (§5,
  `VISION_ARCHITECTURE.md` §13). No data loss; no silent failure.

### 2.2 Four maturity stages (distinct, sequential, each its own gate)

| Stage | What runs | Hardware | Purpose |
|---|---|---|---|
| **A — Near-term NVR ingestion** | NVR adapter only; no first-party models | none (needs Dahua API creds) | Turn existing unused NVR AI reports into alarms + analytics |
| **B — Lab / replay MVP** | Full pipeline on **recorded video**, stub/permissive detector | none (dev laptop/CI) | Prove the pipeline end-to-end with no camera/GPU/weights (§9) |
| **C — First single-camera edge pilot** | One camera, one zone, one rule, real edge node | 1 edge box (CPU-small or GPU-balanced) | Validate real-camera perception + latency on one high-value zone |
| **D — Multi-camera production** | Several cameras/zones, health, evidence, fusion | GPU-balanced/-accuracy + relocated server room | Production security/loss use-cases |

Approving this design authorises **Stage B** against recorded video. **A** unblocks on the Dahua
API/NDA. **C/D** are separate gates requiring the site audit, model selection, and hardware sign-off.

---

## 3. Perception-to-operational boundary

The load-bearing rule: **the model answers "what is in the frame?"; site rules answer "what does
that mean here?"** Each layer below states what it *may* and *must never* produce.

### 3.1 Layer: first-party perception (`vision.observation.v1`)
- **May produce:** object class from the perception ontology (§6.1), normalised bounding box,
  class confidence, tracker `track_id` (identity *within one camera*), image-space motion, optional
  PPE present/absent on a person crop, optional projected `ground_point`.
- **Must never produce:** an operational conclusion (`queueing`, `loading_complete`,
  `unsafe_proximity`), an `asset_id`/fleet identity, a person's identity, a severity, or a claim of
  measurement.
- **Provenance:** always `observed`. Perception never emits `inferred`/`correlated`.
- **Confidence:** per-object class confidence ∈ [0,1], carried verbatim; never rounded to "certain."
- **Evidence:** the frame reference and model id/version are the evidence; retained on every object.
- **Versioning:** every observation embeds `model.id` + `model.version`; the schema is `*.v1`.

### 3.2 Layer: NVR vendor events (`vision.vendor_event.v1`)
- **May produce:** the vendor's own event (line-crossing, intrusion, vehicle), its channel/camera,
  the vendor's timestamp and (if given) confidence, the vendor rule name.
- **Must never produce:** a first-party `observed` claim, a fused `asset_id`, an identity, or a
  severity of its own.
- **Provenance:** `vendor_inferred` (a distinct class — §3.6). Confidence is the vendor's, labelled
  as such, and **never silently upgraded** to first-party confidence.
- **Evidence:** the raw vendor payload (normalised, retained) + optional NVR clip reference.
- **Versioning:** `*.v1`; the adapter records the vendor event-type string it mapped from.

### 3.3 Layer: operational events (`vision.operational_event.v1`)
- **May produce:** a mining-operational conclusion (§6.4) derived by a **named, versioned rule**
  over observations/vendor-events + tracking + zones + time + GNSS.
- **Must never produce:** a conclusion from a single frame; a conclusion without cited evidence; a
  conclusion that upgrades its own provenance; machine control.
- **Provenance:** `inferred` (rule over observations), `correlated` (tied to a GNSS asset), or
  `estimated` (a measured-adjacent computed value, e.g. homography distance).
- **Confidence:** derived from the contributing signals (dwell, proximity, min class conf), ∈ [0,1].
- **Evidence:** **mandatory** — observation window, contributing `track_id`s/frames, zone, and
  per-signal contributions. This is what makes it explainable and recomputable.
- **Versioning:** carries `rule_id` + `rule_version` and the `zone_version`/`calibration_version`
  it used; recomputable from stored observations when a rule improves.

### 3.4 Layer: alarm (`event.v1`, existing — unchanged)
- **May produce:** a human-facing alarm in the unified queue when a rule/zone policy says it
  matters.
- **Must never produce:** a new event framework; a mutation of the underlying observation; a
  non-advisory action.
- **Provenance/confidence:** inherited from the operational event and **rendered on the dashboard**.
- **Severity:** set by the **rule/zone policy**, never by the model.
- **Versioning:** `event.v1`; new vision types are added to the enum additively (already done for
  gate/lab types).

### 3.5 The four distinctions, stated explicitly
- **A detector output is not an operational conclusion.** "haul_truck 0.94" is a pixel fact; "truck
  queueing" is a rule result with evidence.
- **A person detection is not unsafe proximity.** Proximity requires a *second* object, a *distance*
  (calibrated), and *sustained time* — never one person box in one frame.
- **A stationary machine is not a safe machine.** "Not moving" is `stationary` (an observation-derived
  state); "safe to approach" is an operational/safety judgement the platform does **not** make.
- **A visual truck is not an identified fleet asset.** `track_id` is per-camera temporal identity;
  an `asset_id` exists only via fusion, always with an `association` label (`confirmed|probable|
  possible|unknown`), never a silent claim.

### 3.6 NVR provenance is a distinct class
An NVR event and a first-party observation are **not the same provenance**. The vendor's model,
thresholds and failure modes are opaque to us; its confidence is not comparable to ours. The
platform therefore carries `vendor_inferred` as a first-class provenance label, the dashboard
distinguishes it, and a rule may weight it differently from `observed`. Collapsing the two would
present a black-box vendor guess as our own perception — forbidden.

---

## 4. Data contracts (schema-level)

Schema-level specifications for review — **not** production JSON Schema files. On approval they
land in `contracts/` + `src/minemonitor/contracts/` and register in `platform/contracts.py`, exactly
like the existing `*.v1` contracts, and are validated-or-rejected at the ingest boundary (never
silently coerced). Common envelope fields required on every contract are listed once, then per-contract
deltas follow.

**Common envelope (all contracts):** `schema_version` (const per contract), `site_id`, `node_id`
(edge-produced contracts), `camera_id` or `source_system`, `captured_at` (source/edge clock),
`ingested_at` (core arrival — the gap detects buffered backfill), `advisory: true` where the contract
can drive an alarm. Provenance/confidence/evidence/model/rule/zone fields appear where the layer
requires them (§3).

**Global failure behaviour (all contracts):** a payload that fails registry validation is **rejected
and logged loudly with `site_id`/`camera_id`**, never partially stored or coerced. Unknown extra
fields are rejected (`additionalProperties: false`). An unknown `schema_version` is rejected. Malformed
device/vendor data never corrupts derived analytics.

### 4.1 `vision.observation.v1` — raw first-party perception
- **Purpose:** what a model saw in one frame. Authoritative, write-once, the basis for recompute.
- **Producer:** Vision Edge Service (first-party path). **Consumer:** core ingestor → observation
  store → temporal engine.
- **Required:** envelope + `frame_id`, `model{id,version}`, `objects[]` where each object =
  `{track_id, class, confidence∈[0,1], bbox{x1,y1,x2,y2 normalised}, provenance:"observed"}`.
- **Optional:** `image{width,height}`, per-object `ground_point{lat,lon,basis:homography|depth|unknown}`
  (+ `calibration_version`), `attributes{ppe:…}`.
- **Validation:** bbox normalised [0,1]; confidence in [0,1]; `provenance` const `observed`;
  `track_id` namespaced `<camera_id>:<n>` and **never** an `asset_id`.
- **Versioning:** immutable `v1`; a breaking change is `v2` alongside.
- **Retention:** high-volume class; retention-policy-driven window (e.g. [ASSUMPTION] 14–30 days of
  observations); recompute of operational events requires this window, so set it to the longest
  rule look-back you must support.
- **Failure when invalid:** rejected at ingest; the edge keeps the frame's observation locally for
  the buffer window so a fixed schema can re-ingest.
- **Example:**
```json
{
  "schema_version": "vision.observation.v1",
  "site_id": "kn-zw-01", "node_id": "edge-gr-01", "camera_id": "cam-gr-01",
  "captured_at": "2026-09-05T11:42:07Z", "ingested_at": "2026-09-05T11:42:09Z",
  "frame_id": "01J9Z8...", "model": {"id": "mine-equipment-detector", "version": "0.1.0"},
  "image": {"width": 1920, "height": 1080},
  "objects": [{
    "track_id": "cam-gr-01:37", "class": "haul_truck", "confidence": 0.94,
    "bbox": {"x1": 0.31, "y1": 0.44, "x2": 0.52, "y2": 0.71},
    "ground_point": {"lat": -17.8253, "lon": 31.0336, "basis": "homography"},
    "calibration_version": "cam-gr-01/2026-09-22", "attributes": {"ppe": null},
    "provenance": "observed"
  }],
  "advisory": true
}
```

### 4.2 `vision.operational_event.v1` — interpreted, evidenced
- **Purpose:** a mining-operational conclusion derived by a versioned rule. **Producer:** core (or
  edge) rule engine. **Consumer:** `event.v1` promotion, analytics, bus subscribers.
- **Required:** envelope + `event_id`, `type` (operational ontology §6.4), `provenance∈{inferred,
  correlated,estimated}`, `confidence∈[0,1]`, `rule_id`, `rule_version`, `evidence{observation_window,
  track_ids[], frame_ids[], signals{…}}`.
- **Optional:** `subject{track_id, asset_id, association:confirmed|probable|possible|unknown}`,
  `zone_id` + `zone_version`, `evidence.clip_ref`.
- **Validation:** `evidence` non-empty; provenance never `observed`; `asset_id` only with an
  `association` label; confidence in [0,1].
- **Versioning:** `v1`; `rule_id`/`rule_version` make each event traceable to the exact rule.
- **Retention:** derived/recomputable; retention-policy-driven (e.g. [ASSUMPTION] 365 days, matching
  events).
- **Failure when invalid:** rejected; not promoted to `event.v1`; logged with the rule id.
- **Example:**
```json
{
  "schema_version": "vision.operational_event.v1", "event_id": "01J9ZB...",
  "site_id": "kn-zw-01", "node_id": "edge-gr-01", "camera_id": "cam-gr-01",
  "captured_at": "2026-09-05T11:43:20Z", "ingested_at": "2026-09-05T11:43:22Z",
  "type": "unsafe_proximity_person_machine",
  "subject": {"track_id": "cam-gr-01:41", "asset_id": "HT-102", "association": "probable"},
  "zone_id": "r-load-1", "zone_version": "2026-09-22",
  "provenance": "estimated", "confidence": 0.82,
  "rule_id": "unsafe_proximity_person_machine", "rule_version": "1.0.0",
  "evidence": {
    "observation_window": ["2026-09-05T11:43:14Z", "2026-09-05T11:43:20Z"],
    "track_ids": ["cam-gr-01:41", "cam-gr-01:37"], "frame_ids": ["...", "..."],
    "signals": {"min_distance_m": 5.6, "sustained_ms": 4200, "min_conf": 0.71},
    "clip_ref": null
  },
  "advisory": true
}
```

### 4.3 `vision.health.v1` — edge-node health
- **Purpose:** liveness/throughput of an edge node so a dead/degraded node is visible.
  **Producer:** Vision Edge Service. **Consumer:** core health view, alerting.
- **Required:** envelope (`node_id`) + `status∈{ok,degraded,down}`, `heartbeat_at`, `cameras_active`,
  `fps_mean`, `queue_depth`, `dropped_frames`.
- **Optional:** `cpu_pct`, `mem_pct`, `gpu_pct`, `vram_pct`, `model_ids[]`, `last_error`.
- **Validation:** status enum; numeric ranges sane; heartbeat monotonic per node.
- **Versioning:** `v1`. **Retention:** short (e.g. [ASSUMPTION] 30 days) — operational telemetry.
- **Failure when invalid:** rejected; a missing heartbeat beyond `MM_HEARTBEAT_STALE_S`-style
  threshold marks the node `down` in the health view (absence is itself a signal).

### 4.4 `vision.camera_health.v1` — per-camera health
- **Purpose:** per-camera usability (obstruction, defocus, black frame, signal loss) — distinct from
  node health. **Producer:** edge (cheap frame-quality checks, not a heavy model). **Consumer:**
  health view; may drive a `camera_obstructed` operational event.
- **Required:** envelope (`camera_id`, `node_id`) + `state∈{ok,obstructed,defocused,dark,no_signal,
  unknown}`, `assessed_at`.
- **Optional:** `blur_score`, `luma_mean`, `frozen_frames`, `last_frame_at`.
- **Validation:** state enum; scores in documented ranges.
- **Versioning:** `v1`. **Retention:** short (e.g. [ASSUMPTION] 30–90 days).
- **Failure when invalid:** rejected; `no_signal`/absence handled as camera-offline in the health
  view.

### 4.5 `vision.vendor_event.v1` — NVR-normalised event (near-term path)
- **Purpose:** a normalised Dahua NVR AI report, explicitly **vendor-inferred** (§3.2/§3.6).
  **Producer:** NVR adapter. **Consumer:** optional operational-event mapping → `event.v1`.
- **Required:** envelope (`source_system:"dahua_nvr"`, `camera_id`) + `vendor_event_id`,
  `vendor_type` (raw string, e.g. `LineDetection`), `normalized_type` (§6.4/§8), `provenance:
  "vendor_inferred"`, `vendor_captured_at`.
- **Optional:** `vendor_confidence` (labelled as vendor's), `vendor_rule_name`, `bbox` (if provided),
  `channel`, `clip_ref`.
- **Validation:** `provenance` const `vendor_inferred`; `vendor_confidence` **never** copied into a
  first-party confidence field; `camera_id` normalised via the adapter's channel→camera map (§8).
- **Versioning:** `v1`; the adapter records the vendor firmware/API version it read.
- **Retention:** events-class (e.g. [ASSUMPTION] 365 days).
- **Failure when invalid:** rejected and logged; a missing required vendor field is represented as
  `null` + a `field_missing` note, never fabricated.
- **Example:**
```json
{
  "schema_version": "vision.vendor_event.v1", "site_id": "kn-zw-01",
  "source_system": "dahua_nvr", "camera_id": "cam-gate-02",
  "vendor_event_id": "nvr-88231", "vendor_type": "CrossLineDetection",
  "normalized_type": "nvr_line_crossing", "vendor_rule_name": "Gate-Line-A",
  "vendor_confidence": 0.77, "vendor_captured_at": "2026-09-05T11:41:59+02:00",
  "captured_at": "2026-09-05T09:41:59Z", "ingested_at": "2026-09-05T09:42:03Z",
  "provenance": "vendor_inferred", "clip_ref": "nvr://cam-gate-02/88231", "advisory": true
}
```

### 4.6 `vision.evidence_clip_reference.v1` — evidence pointer (never a stream)
- **Purpose:** point to a short clip/still retained on the edge or in MinIO, so an operator can see
  *why* without video crossing the link continuously. **Producer:** edge/NVR adapter. **Consumer:**
  `event.v1.evidence`, dashboard on-demand fetch.
- **Required:** envelope + `clip_id`, `uri` (`s3://…` or `nvr://…`), `start_at`, `end_at`,
  `retention_class`.
- **Optional:** `duration_ms`, `width`, `height`, `sha256`, `still_uri`.
- **Validation:** `uri` scheme allow-listed; window sane; **no inline video bytes** in the contract.
- **Versioning:** `v1`. **Retention:** clip class, per-site deletable (e.g. [ASSUMPTION] 7–30 days of
  clips; the *reference* may outlive the clip and resolve to "expired").
- **Failure when invalid:** rejected; a dangling reference resolves to `expired`/`unavailable`, never
  a broken player.

### 4.7 `event.v1` mapping from vision (existing contract — unchanged)
- **Purpose:** promote an operational (or notable vendor) event into the unified alarm queue.
  **Producer:** core. **Consumer:** alarm queue, notifications, dashboard.
- **Mapping rules:** `source = "vision:<camera_id>"` (first-party) or `"nvr:<camera_id>"` (vendor);
  `type` from the existing enum (`proximity`, `restricted_area`-style, additive new types as needed);
  `severity` from **rule/zone policy**, not the model; `evidence` links back to the
  `vision.operational_event.v1` id (+ provenance + confidence) and any `clip_ref`; `detail` carries
  `association`/`confidence`/`distance_m` as applicable; `advisory: true` always.
- **Validation/versioning/retention/failure:** the existing `event.v1` rules — unchanged. New enum
  values are additive and versioned.
- **Example:**
```json
{
  "schema": "event.v1", "event_id": "01J9ZB...", "site_id": "kn-zw-01",
  "ts": "2026-09-05T11:43:20Z", "type": "proximity", "severity": "critical",
  "asset_id": "HT-102", "zone_id": "r-load-1", "source": "vision:cam-gr-01",
  "summary": "Person within 6 m of moving haul truck HT-102 at ROM loading face",
  "detail": {"association": "probable", "confidence": 0.82, "distance_m": 5.6, "provenance": "estimated"},
  "evidence": {"vision_operational_event_id": "01J9ZB...", "clip_uri": "s3://.../clip.mp4"},
  "advisory": true, "state": "open"
}
```

### 4.8 `vision.rule_version.v1` — rule/config metadata (reproducibility anchor)
- **Purpose:** record the exact temporal-rule configuration a deployment ran, so any operational
  event is reproducible from stored observations + the rule that made it. **Producer:** admin
  config/deploy. **Consumer:** the rule engine; audit; recompute.
- **Required:** envelope + `rule_id`, `rule_version` (semver), `event_type`, `parameters{…}` (the §7
  temporal profile), `zone_version`, `active_from`.
- **Optional:** `active_to`, `author`, `notes`, `calibration_version`, `hardware_profiles[]`.
- **Validation:** semver; `parameters` schema-checked against §7; a rule referenced by an operational
  event must exist in the registry.
- **Versioning:** immutable per `(rule_id, rule_version)` — a change is a new version, never an
  in-place edit (mirrors the model registry rule).
- **Retention:** kept as long as any event that cites it (accountability outlives the data).
- **Failure when invalid:** an operational event citing an unknown `(rule_id, rule_version)` is
  rejected — no orphaned, unexplainable conclusions.
- **Example:**
```json
{
  "schema_version": "vision.rule_version.v1", "site_id": "kn-zw-01",
  "rule_id": "person_in_restricted_zone", "rule_version": "1.2.0",
  "event_type": "person_in_restricted_zone", "zone_version": "2026-09-22",
  "parameters": {"min_observations": 3, "min_dwell_ms": 2000, "hysteresis_observations": 3,
                 "cooldown_ms": 60000, "window_ms": 5000, "confidence_threshold": 0.5,
                 "severity": "critical"},
  "active_from": "2026-10-01T00:00:00Z", "author": "adeel", "advisory": true
}
```

---

## 5. Observation volume & transport policy

Per-frame observations are **too high-volume to publish continuously over MQTT** on a thin mine
link (e.g. 5 cameras × 5 fps × several objects = tens of messages/second, most of them uneventful).
The policy separates *storage* from *transport*.

- **Storage (edge, always):** every observation is written to a **local, crash-safe store on the
  edge node** (the record of what was seen), independent of what is transmitted. [ASSUMPTION] local
  store = an embedded DB (e.g. SQLite/DuckDB-class) or an append-only log; final choice is a build
  decision, not a design commitment here.
- **Transport — production default (recommended):** the edge **does not** stream raw observations to
  the core. It transmits:
  1. **Operational events** (`vision.operational_event.v1`) — low-volume, the things that matter.
  2. **Periodic rollups** — small per-camera/zone summaries (counts, occupancy, dwell histograms)
     on a fixed cadence (e.g. [ASSUMPTION] every 5 min, aligning with `asset.metrics.v1` buckets).
  3. **Health** (`vision.health.v1` / `vision.camera_health.v1`) on a heartbeat cadence.
  4. **On-demand observation slices** — the core can *request* the raw observations behind a
     specific operational event (for audit/recompute); they are pulled, not pushed.
- **Transport — lab/debug mode:** the edge **may** publish full per-frame observations for a bounded
  session, so the pipeline can be inspected end-to-end during development/commissioning. Explicitly a
  non-production mode, rate-limited and time-boxed.
- **QoS & backpressure:** operational events and health use **MQTT QoS 1** (at-least-once; the core
  dedupes on id — idempotent ingest). If the broker/core is unreachable, the edge **buffers locally
  and backfills on reconnect** (the M2 store-and-forward bar), applying backpressure by dropping
  *oldest raw frames first* while **never** dropping an operational event or its evidence.
- **What is sufficient for recomputation:** the **stored observations + the versioned rule**
  (`vision.rule_version.v1`) are sufficient to recompute operational events. So the observation
  retention window must be ≥ the longest rule look-back you must support retrospectively.
- **What cannot be recomputed without raw frames:** anything requiring re-running the *model*
  (a better detector, a new class, re-scoring confidence) needs the frames — which are **not**
  retained beyond the edge ring buffer. This is an accepted trade: we can recompute *rules*, not
  *perception*. If perception re-scoring is ever needed, it is a deliberate, consented,
  retention-bounded frame-capture exercise, not the steady state.
- **Network-outage buffering:** edge buffers operational events + health locally (crash-safe) and
  backfills in order on reconnect; late/out-of-order arrivals are tolerated (idempotent, re-derivable).
- **Maximum acceptable event latency:** [ASSUMPTION, for review] frame→observation ≤ 750 ms
  (CPU-small) to ≤ 150 ms (GPU-accuracy); observation→`event.v1` ≤ 1 s; a **safety-relevant**
  operational event (unsafe proximity) end-to-end ≤ 2 s on its supported profile. Latency budgets are
  per §12 of `VISION_ARCHITECTURE.md` and **measured, not asserted**.
- **[OPEN QUESTION]** Confirm the sustained uplink budget on the single Starlink link and the number
  of cameras/zones in scope, to size the rollup cadence and buffer.

**Recommended production default:** *store all observations on the edge; transmit only operational
events + 5-min rollups + health; pull raw observation slices on demand; QoS 1 with local backfill.*

---

## 6. Ontology (versioned)

All ontologies are **published as data** (`contracts/vision.ontology.v1.json` when implemented), so
classes/types are added without a code change — exactly as zone rules are data today. Each is
versioned; a model card / rule declares which version it targets.

### 6.1 Detected object classes (perception ontology)
`person` · `haul_truck` · `light_vehicle` · `loader` · `excavator` · `dozer` · `grader` · `drill` ·
`water_truck` · `service_truck` · `bus`. *(Identity out of scope — `person`, never "who".)*

### 6.2 Machinery classes
The mobile-plant subset of §6.1: `haul_truck, light_vehicle, loader, excavator, dozer, grader,
drill, water_truck, service_truck`.

### 6.3 Static infrastructure (usually zones, not detections)
`conveyor` · `stockpile` · `dump_point` · `crusher` · `loading_face` · `haul_road` · `intersection` ·
`maintenance_area` · `weighbridge`. **Design note:** static features are represented as **configured
zones**, not per-frame detections, unless they genuinely move/change.

### 6.4 Operational event types
`zone_entry` · `zone_exit` · `person_in_restricted_zone` · `vehicle_in_restricted_zone` ·
`unsafe_proximity_person_machine` · `machine_stationary_in_queue_zone` · `machine_loading_started`* ·
`machine_loading_complete`* · `machine_offloading_detected`* · `nvr_line_crossing_normalized` ·
`nvr_intrusion_normalized` · `camera_obstructed` · `zone_occupancy_exceeded`. *(\* = deferred/estimated
— see §6.7.)*

### 6.5 Zones
Reuse the existing zone `kind`s and add security kinds as data: `loading` · `unloading` ·
`restricted` · `exclusion_zone` · `speed_limited` · `queue_zone` · `haul_road` · `intersection` ·
`maintenance_area` · `generic`. Camera image-zones map to these operational zones (with a
`zone_version`).

### 6.6 Severity · provenance · confidence · uncertainty · lifecycle
- **Severity:** `info` · `warning` · `critical` (existing `event.v1` enum) — set by rule/zone policy.
- **Provenance:** `observed` · `vendor_inferred` · `inferred` · `correlated` · `estimated` · `unknown`.
- **Confidence ranges (labels for display, thresholds set per rule):** `very_low [0,0.3)` ·
  `low [0.3,0.5)` · `medium [0.5,0.7)` · `high [0.7,0.9)` · `very_high [0.9,1.0]`. Raw float always
  retained; label is presentation only.
- **Uncertainty classes:** `insufficient_evidence` (below min observations/dwell) · `ambiguous_track`
  (id switch/occlusion) · `no_calibration` (no homography → no distance) · `vendor_opaque` (NVR event,
  unknown model) · `stale` (beyond freshness window).
- **Event lifecycle:** the existing `event.v1` states — `open` → `acknowledged` → `resolved` (with
  incident escalation available); vision changes nothing here.

### 6.7 Machinery states (with evidence discipline)
State machine per track; **telemetry-optional but labelled**. For each state: evidence *required*,
corroborating evidence *optional*, *prohibited* inference, min dwell, hysteresis, confidence
threshold, allowed transitions, telemetry requirement.

| State | Evidence required | Corroborating (optional) | Prohibited inference | Min dwell | Hysteresis | Conf thresh | Allowed transitions | Telemetry |
|---|---|---|---|---|---|---|---|---|
| `moving` | image-space displacement over N frames | GNSS speed > 0 | "hauling loaded" (payload unknown) | 1.5 s | 3 obs | 0.5 | →stationary, reversing | helpful |
| `stationary` | no displacement over window | GNSS speed ≈ 0 | "safe"/"parked"/"idle-engine" | 3 s | 3 obs | 0.5 | →moving, active_but_stationary, queueing, parked | helpful |
| `reversing` | motion vector opposite heading | reverse light class (if trained) | "unsafe" (needs proximity rule) | 1 s | 2 obs | 0.6 | →moving, stationary | helpful |
| `queueing` | `stationary` **in a `queue_zone`**, behind another machine | GNSS position | "loading" | 20 s | 3 obs | 0.5 | →moving, loading | helpful |
| `parked` | `stationary` in a `maintenance_area`/park zone, extended | ignition-off event (GNSS) | "offline"/"broken" | 120 s | 5 obs | 0.5 | →moving | helpful |
| `active_but_stationary` | `stationary` but articulation/tool motion (if detectable) OR GNSS ignition-on | — | "idle"/"working done" | 5 s | 3 obs | 0.5 | →moving, stationary | **required** to assert reliably; else `unknown` |
| `loading`* | proximity to loader/excavator + dwell in `loading` zone | GNSS, payload sensor | "loaded"/tonnes | 15 s | 3 obs | 0.6 | →loaded, moving | **required** for high confidence; else `estimated` |
| `offloading`* | dwell in `dump_point` zone + body-tip motion (if detectable) | GNSS, tip sensor | "tonnes dumped" | 10 s | 3 obs | 0.6 | →moving | **required**; else `estimated`/deferred |
| `unknown` | insufficient/ambiguous evidence | — | any positive state | — | — | — | →any on new evidence | n/a |

**Overpromise guard (loading/offloading, `*`):** without machine-part detection, keypoints, payload
sensors, or OEM telemetry, `loading`/`offloading`/`loading_complete` are **`estimated` at best and
deferred to a later phase**. This design does **not** promise reliable loading detection from a
generic detector; it promises to *flag a plausible loading interaction with cited evidence*, labelled
`estimated`, or to defer. `[OPEN QUESTION]` availability of OEM telemetry / payload sensors on the
RAN Mines fleet (also a §14 open question in `CLAUDE.md`).

---

## 7. Temporal rule framework

Rules are **data, not code** (like zone rules today), each with its **own temporal profile**. YAML-style
pseudocode below — **not executable**. Every rule carries `rule_id` + `rule_version` and is recorded
as `vision.rule_version.v1` (§4.8).

**Why a single global dwell threshold is unsafe.** A restricted-zone entry must fire in ~1–2 s (a
person is already where they must not be); an unsafe-proximity alarm must fire in well under a second
to be useful, but must not fire on a single jittered box; a queueing state needs *tens of seconds* of
dwell or every red light triggers it; a camera-obstruction check should wait many seconds before
crying wolf. One global threshold either drowns the control room in false alarms (too low for
queueing) or misses safety events (too high for proximity). **Each event type needs its own
`min_observations / min_dwell / hysteresis / cooldown / window / confidence` profile**, tuned to its
cost of a false positive vs. a missed detection.

```yaml
# ── Safety / security (fast, low miss-tolerance) ───────────────────────────
- rule_id: person_in_restricted_zone           # 1
  event_type: person_in_restricted_zone
  min_observations: 3
  min_dwell_ms: 1500
  hysteresis_observations: 3          # must be clear this many obs before "exited"
  cooldown_ms: 60000                  # re-alarm suppression per (track, zone)
  window_ms: 5000
  confidence_threshold: 0.5
  severity: critical
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 6000
  allowed_profiles: [cpu_small, gpu_balanced, gpu_accuracy]

- rule_id: vehicle_in_restricted_zone           # 2
  event_type: vehicle_in_restricted_zone
  min_observations: 3
  min_dwell_ms: 2000
  hysteresis_observations: 3
  cooldown_ms: 60000
  window_ms: 5000
  confidence_threshold: 0.5
  severity: critical
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 6000
  allowed_profiles: [cpu_small, gpu_balanced, gpu_accuracy]

- rule_id: unsafe_proximity_person_machine      # 3  (needs calibration → distance)
  event_type: unsafe_proximity_person_machine
  requires: [ground_calibration]      # else provenance=unknown, no alarm
  min_observations: 4
  min_dwell_ms: 800                    # sustained, but fast
  hysteresis_observations: 4
  cooldown_ms: 30000
  window_ms: 3000
  distance_threshold_m: 8              # [ASSUMPTION] tune on site + machine class
  confidence_threshold: 0.6
  severity: critical
  dedupe_key: [camera_id, person_track_id, machine_track_id]
  evidence_window_ms: 4000
  allowed_profiles: [gpu_balanced, gpu_accuracy]   # distance needs decent fps

# ── Operational (slower, tolerant of latency) ──────────────────────────────
- rule_id: machine_stationary_in_queue_zone     # 4
  event_type: machine_stationary_in_queue_zone
  min_observations: 6
  min_dwell_ms: 20000
  hysteresis_observations: 4
  cooldown_ms: 120000
  window_ms: 30000
  confidence_threshold: 0.5
  severity: info
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 30000
  allowed_profiles: [cpu_small, gpu_balanced, gpu_accuracy]

- rule_id: machine_loading_started              # 5  (ESTIMATED — see §6.7)
  event_type: machine_loading_started
  provenance: estimated
  requires_any: [loader_proximity, telemetry]   # else defer, do not emit
  min_observations: 6
  min_dwell_ms: 15000
  hysteresis_observations: 4
  cooldown_ms: 120000
  window_ms: 30000
  confidence_threshold: 0.6
  severity: info
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 30000
  allowed_profiles: [gpu_balanced, gpu_accuracy]

- rule_id: machine_loading_complete             # 6  (ESTIMATED — see §6.7)
  event_type: machine_loading_complete
  provenance: estimated
  requires_any: [loader_proximity, telemetry]
  min_observations: 4
  min_dwell_ms: 5000                   # departure after a loading interaction
  hysteresis_observations: 3
  cooldown_ms: 120000
  window_ms: 60000
  confidence_threshold: 0.6
  severity: info
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 60000
  allowed_profiles: [gpu_balanced, gpu_accuracy]

- rule_id: machine_offloading_detected          # 7  (ESTIMATED/DEFERRED — see §6.7)
  event_type: machine_offloading_detected
  provenance: estimated
  requires_any: [tip_motion, tip_sensor, telemetry]   # else defer
  min_observations: 4
  min_dwell_ms: 8000
  hysteresis_observations: 3
  cooldown_ms: 120000
  window_ms: 30000
  confidence_threshold: 0.6
  severity: info
  dedupe_key: [camera_id, zone_id, track_id]
  evidence_window_ms: 30000
  allowed_profiles: [gpu_balanced, gpu_accuracy]

# ── NVR-normalised (vendor_inferred provenance) ────────────────────────────
- rule_id: nvr_line_crossing_normalized         # 8
  event_type: nvr_line_crossing_normalized
  source: vision.vendor_event.v1
  provenance: vendor_inferred
  min_observations: 1                  # NVR event is already an event, not a frame
  min_dwell_ms: 0
  hysteresis_observations: 0
  cooldown_ms: 30000                   # dedupe vendor repeats
  window_ms: 0
  confidence_threshold: 0.0            # vendor confidence carried, not gated by us
  severity: warning
  dedupe_key: [source_system, camera_id, vendor_event_id]
  evidence_window_ms: 0
  allowed_profiles: [nvr]

- rule_id: nvr_intrusion_normalized             # 9
  event_type: nvr_intrusion_normalized
  source: vision.vendor_event.v1
  provenance: vendor_inferred
  min_observations: 1
  min_dwell_ms: 0
  hysteresis_observations: 0
  cooldown_ms: 30000
  window_ms: 0
  confidence_threshold: 0.0
  severity: warning
  dedupe_key: [source_system, camera_id, vendor_event_id]
  evidence_window_ms: 0
  allowed_profiles: [nvr]

# ── Camera health ──────────────────────────────────────────────────────────
- rule_id: camera_obstructed                    # 10
  event_type: camera_obstructed
  source: vision.camera_health.v1
  provenance: inferred
  min_observations: 10                 # many consecutive bad-quality assessments
  min_dwell_ms: 30000                  # obstructed for ≥30 s before crying wolf
  hysteresis_observations: 10
  cooldown_ms: 600000
  window_ms: 60000
  confidence_threshold: 0.7
  severity: warning
  dedupe_key: [camera_id]
  evidence_window_ms: 60000
  allowed_profiles: [cpu_small, gpu_balanced, gpu_accuracy]
```

Notes: `dedupe_key` prevents alarm storms (mirrors GNSS debounce); `cooldown_ms` prevents re-alarm
churn on the same subject; `requires`/`requires_any` gate a rule so it degrades to `unknown`/defer
rather than guessing when its inputs are absent; `allowed_profiles` documents which hardware can run
the rule at the fps it needs (proximity/loading need GPU-class fps; restricted-zone can run on
CPU-small).

---

## 8. NVR adapter contract (near-term Dahua path)

The near-term, no-new-hardware path. It is **vendor event ingestion, not first-party perception**
(§3.6), and reuses the scaffolded `nvr.py` / `dahua_nvr_sim.py` normaliser-then-driver shape.

- **Discovery & ingestion.** The adapter polls the Dahua NVR AI-report API on a cadence (cursor =
  last-seen `vendor_event_id`/timestamp; refetch a small overlap and dedupe — the exact pattern the
  Dahua gate poller already uses). `[OPEN QUESTION]` the real API: endpoint, auth (the "backdoor"
  creds via Heath), pagination, event schema, whether push/subscribe is available vs. poll-only.
- **Vendor → platform type mapping.** A small, versioned map from vendor event strings to
  `normalized_type` (e.g. `CrossLineDetection → nvr_line_crossing`, `CrossRegionDetection →
  nvr_intrusion`, `TrafficJunction`/vehicle → `nvr_vehicle`). Isolated to **one function** (the
  `_to_raw`/`_map_vendor_type` seam) so the real vendor strings are confirmed on site and changed
  **there only**.
- **Confidence mapping.** Vendor confidence (if present) is carried as `vendor_confidence`, labelled
  `vendor_inferred`, and **never** written into a first-party `confidence` field. If absent, it is
  `null` — never fabricated.
- **Camera-id normalisation.** The NVR channel/GUID is mapped to the platform `camera_id` via the
  camera registry (FP-01). An unmapped channel is ingested with `camera_id` = a deterministic
  `dahua_nvr:<channel>` placeholder and flagged for registry linkage — never dropped.
- **Zone-id representation.** The vendor's line/region name maps to an operational `zone_id` where a
  mapping exists (with `zone_version`); otherwise the vendor rule name is retained in evidence and no
  zone is asserted.
- **Timestamps & clock drift.** Store both `vendor_captured_at` (NVR clock) and `ingested_at` (core
  clock); the gap detects drift/backfill. `[OPEN QUESTION]` confirm NVR is NTP-synced; if not, record
  an estimated skew and label correlations accordingly (a skewed NVR clock corrupts reconciliation).
- **Deduplication.** Idempotent on `dedupe_key = (source_system, camera_id, vendor_event_id)`; a
  re-poll of the overlap window never double-alarms (the platform's existing idempotent-ingest bar).
- **Missing fields.** Represented as `null` + a `field_missing` note in evidence; never invented,
  never coerced.
- **Adapter health.** The adapter emits its own `vision.health.v1` (node = the adapter) — poll
  success/failure, last-seen event age, lag — so a stalled adapter is visible.
- **NVR unavailable.** On API failure the adapter logs, backs off, and retries (bounded, with
  backoff); no crash, no data loss (it resumes from the cursor). The core is unaffected. Absence
  beyond a threshold marks the adapter `down` in the health view.
- **Provenance labelling.** Every NVR event is `vendor_inferred` end-to-end and rendered distinctly
  on the dashboard — never shown as first-party `observed`.

**Example mapping chain:**
```text
Dahua "CrossLineDetection" on channel 2, conf 0.77, 11:41:59+02:00
  → vision.vendor_event.v1 { source_system:"dahua_nvr", camera_id:"cam-gate-02",
      vendor_type:"CrossLineDetection", normalized_type:"nvr_line_crossing",
      vendor_confidence:0.77, provenance:"vendor_inferred", clip_ref:"nvr://…" }
  → (rule nvr_line_crossing_normalized, cooldown 30s, dedupe by vendor_event_id)
     vision.operational_event.v1 { type:"nvr_line_crossing_normalized",
       provenance:"vendor_inferred", rule_id/rule_version, evidence:{vendor payload, clip_ref} }
  → event.v1 { type:"zone_breach"|new additive type, severity:"warning",
       source:"nvr:cam-gate-02", detail:{provenance:"vendor_inferred", vendor_confidence:0.77},
       evidence:{vision_operational_event_id, clip_uri}, advisory:true }
```

---

## 9. Hardware-free MVP (Stage B) and acceptance

The smallest end-to-end slice, buildable with **no live camera, no GPU, no site hardware, no model
weights** — a **stub/permissive adapter over recorded video** (CI stays weights-free and GPU-free,
per `VISION_ARCHITECTURE.md` §13).

```
recorded clip → stub detector (emits fixture detections) → tracker (stable track_id)
  → one configured image-zone (restricted) → vision.observation.v1 (stored, versioned)
  → temporal rule person_in_restricted_zone (§7 #1) → vision.operational_event.v1
  → event.v1 (source: "vision:cam-sample") → existing alarm queue + SSE dashboard
```

**Acceptance (mirrors the M3 geofence bar):**
1. A `person` track dwelling in the configured zone raises **exactly one** `event.v1` (debounced),
   visible and acknowledgeable in the existing dashboard.
2. A boundary-hugging or briefly-occluded track raises **none** (hysteresis holds).
3. The observation is stored with `model.id/version` + `provenance:observed` and is **replayable**;
   re-running the clip double-alarms **zero** times (idempotent).
4. The operational event carries `rule_id/rule_version` + full evidence and is **recomputable** from
   stored observations (change the rule version → recompute → new result, old event preserved).
5. The core runs **identically** whether the edge slice is present or absent.
6. A malformed observation is **rejected loudly**, never coerced.
7. **No identity, no biometric field, no raw frame** ever crosses to the core; only the structured
   contracts (+ optional clip *reference*).

Explicitly **not** in the MVP: multi-class detection, segmentation, GPS fusion, PPE, proximity,
loading/offloading, real cameras, GPU, training, model selection. Each is a later, separately-gated
increment.

---

## 10. Build-gate checklist

The design is gate-ready when a reviewer can answer **yes** to all of:
- [ ] The two paths (NVR ingestion vs. first-party perception) and the four maturity stages (§2) are
  agreed, with NVR-first as the sequence.
- [ ] The perception↔operational boundary (§3) and the four distinctions are accepted as binding.
- [ ] The eight contracts (§4) are accepted as the build spec (schema-level), including
  `vendor_inferred` as a distinct provenance class.
- [ ] The transport policy (§5) — store-on-edge, transmit events+rollups+health, pull observations
  on demand — is accepted as the production default.
- [ ] The ontology (§6) and the loading/offloading overpromise guard (§6.7) are accepted.
- [ ] The per-event temporal framework (§7) is accepted, with per-event profiles (no global dwell).
- [ ] The NVR adapter contract (§8) is accepted, pending the vendor API spec.
- [ ] The hardware-free MVP (§9) is authorised as the first increment.
- [ ] The open questions (§11) are acknowledged as gating C/D, not this design.

---

## 11. Open questions and assumptions (do not guess — resolve on the site visit / vendor spec)

**[OPEN QUESTION]s (site/vendor facts we must not invent):**
1. Dahua NVR AI-report API — endpoint, auth (backdoor creds via Heath), event schema, poll vs.
   push, pagination, firmware/API version.
2. Exact Dahua vendor event-type strings (for the §8 mapping seam).
3. Is the NVR NTP-synced? Clock skew corrupts reconciliation and correlation.
4. Per-camera inference suitability at distance / low light / dust (decides which security use-cases
   the existing cameras can carry vs. where new cameras/angles are needed) — the FP-01 audit.
5. Sustained uplink budget on the single Starlink link; number of cameras/zones in scope (sizes §5).
6. Server-room relocation timeline and power/space for a GPU edge box (gates Stage C/D).
7. Fleet OEM telemetry / payload sensors availability (gates reliable loading/offloading — §6.7).
8. Ground-plane calibration per camera (homography) — required before any distance/proximity rule.

**[ASSUMPTION]s used above, flagged for confirmation:** observation retention 14–30 days; operational
events/vendor events 365 days; clips 7–30 days; health 30–90 days; rollup cadence 5 min; proximity
distance threshold 8 m; latency budgets per §5/§12. All are configuration, not architecture — a
stricter answer costs config, not a redesign.

---

## 12. The lines that do not move (restated, binding)

- **Advisory only.** Every vision output carries `advisory: true`. Vision detects, classifies,
  alerts, recommends, and evidences. It **never** actuates plant. A requirement to cross this line is
  escalated, not implemented (`CLAUDE.md` §15).
- **No biometric templates, ever** (`CLAUDE.md` §4). Perception emits `person`, not identity. Face
  templates, if a consented gate terminal exists, stay in the vendor appliance; the platform ingests
  only an access *event*.
- **Raw video stays on the edge.** Only structured events and referenced clips cross the link.
- **No AGPL model** (incl. Ultralytics YOLO) may sit behind the adapter without explicit legal
  approval; the adapter interface is permissive-only.
- **STOP.** No model, weights, or edge-service code is produced or assumed by this document. Build
  begins only on review and approval of this design.
```
