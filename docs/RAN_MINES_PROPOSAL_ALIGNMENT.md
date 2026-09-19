# Mine Monitor — RAN Mines Proposal Alignment

**Status: alignment pass, for review. This is a planning delta — no significant code
changed. It reconciles the existing platform and the existing Mine Monitor Vision
documentation against the Aurora Gold / RAN Mines proposal (Bindura, dated 13 Sep 2026,
following the 11 Sep 2026 site visit), and sets the implementation sequence.**

Companion feature plans live in [`docs/feature-plans/`](feature-plans/) (index in §7).
The vision architecture docs (`VISION_ARCHITECTURE.md` and siblings) remain valid as the
*edge-perception* reference; this document re-prioritises them and adds the
integration/reconciliation layers the proposal makes central.

> **Flockvision is unrelated** — a previous, separate AI vision project, not part of
> Mine Monitor and not referenced, imported, or depended on anywhere here. Mine Monitor's
> own vision subsystem is **Mine Monitor Vision**. (Verified: no `flockvision` reference
> remains in `docs/`, `src/`, or `contracts/`.)

---

## 0. Requirements-meeting update (14 Sep 2026)

A subsequent team requirements meeting (the site is called **"Randmine"** in those minutes =
this same RAN Mines) materially updates the picture. This section records the deltas; the rest
of the document remains valid where not contradicted here. Two new standalone deliverables sit
in [`docs/assessments/`](assessments/): the **Phase-1 personnel-tracking assessment** and the
**questions for Derek**.

**Facts that change our assumptions**
- **Cameras: 118 Alhua** (not 106/50), a *closed* system with **no public API** — but a vendor
  **"backdoor" API is on offer** (via Heath, who installed the CCTV) and the **NVR already has
  unused AI reports / vehicle-tracking**. → The primary camera-integration path is now the
  **Alhua API + NVR AI-event ingest**; RTSP/ONVIF sub-stream is the fallback. (Updates FP-01/02.)
- **Theft mechanism named:** hand-coating in high-concentrate gold **slurry (dust, not
  nuggets)**, post-crusher/post-cyanide. Client preference = **prevention via access control +
  geofence headcount** ("6 people in a 5-person zone = breach") + **vision + physical tag**
  combination (RFID/tear-off preferred; BLE has a helmet-swap tamper risk). → Sharpens FP-03/04/05.
- **99.9% accuracy demanded before commit** — no single-modality vision meets this. Reframe to
  **measured, per-capability targets + a one-month learning period**; count-reconciliation
  (vision vs tag vs access) is the reliable signal, not single-camera identity through masks.
- **New domains:** 5× **DSE generators** (fuel + run-hours — see new `GENERATOR_MONITORING.md`,
  FP-11); **conveyor auto-adjust to 38 t/h** (⚠ **OUT of scope — crosses the advisory-only
  line**; we monitor/alert, we do not control plant); tank-level + concentrate-box **leak
  detection** (IR, if the liquid runs at a different temperature).
- **Lab confirmed = Agilent 2000-series spectrometer** (vindicates the AAS/SpectrAA call in
  FP-08), now the client's **Phase 4**.
- **Infra:** single **Starlink** (on-prem only, reinforced); **server room is a container next
  to a kitchen — relocation flagged**; **241 staff + 166 contractors**, 24/7 two-shift; **iHUA
  visitor tag** + facial recognition at the gate; alerts wanted on **WhatsApp**.
- **Access still gated:** NDA unsigned, Alhua API not yet granted → no real data yet.

**Client phase order ≠ our engineering sequence.** They are different axes — map, don't
conflate. Our engineering order stays value/risk/hardware-ranked (§5, §7).

| Client phase (their priority) | Maps to our feature plans / slices |
|---|---|
| **P1** — entry security + personnel zone-tracking | FP-07 access, FP-03 counting, FP-04 security-behaviour, FP-05 tag reconciliation, FP-01 camera audit |
| **P2** — drones + perimeter breach | (later phase; separate — vision-edge principles apply) |
| **P3** — conveyor/process automation | **monitor/alert only**; auto belt-control is **OUT** (advisory line) |
| **P4** — generators + lab | FP-11 generators, FP-08 lab (Agilent 2000-series) |

**Out of Mine Monitor entirely** (company/BD context in the minutes): the team website / Lesotho
event, Harris Auto, Finn & Wild, the rhino-poaching pipeline, and Notion/WhatsApp workspace admin.

---

## 1. The headline change in direction

The proposal reframes the product. It is **not** "add computer vision". It is:

> **An integration and intelligence layer that detects, reconciles and surfaces
> inconsistencies across the mine's cameras, access systems, scales, laboratory data and
> operational systems — so loss, security problems and abnormal behaviour become visible
> quickly.**

Three consequences for our roadmap:

1. **Integration before perception.** The mine already owns 106 cameras, recorders, gates,
   turnstiles, a metal detector, a payroll/face system, crusher weightometers, and lab
   instruments. The commercial value is in *connecting and reconciling* these, not in a
   standalone camera-AI product. Gate security + laboratory ingestion (proposal Phase 1)
   come **before** vision (proposal Phase 2).
2. **Reconciliation is a first-class product.** Gold reconciliation across
   ore → crusher → mill → tanks → carbon → furnace → output → gate (proposal Phase 3) is a
   headline capability, not a nice-to-have. It sits on the same "measured vs calculated vs
   inferred, flag-don't-conclude" discipline the platform already uses for fuel/weighbridge.
3. **Vision shifts from fleet to security.** The vision use-cases that matter here are
   **people/security in fixed camera zones** (headcount, area presence, two-person rule,
   dwell, restricted areas, scheduled-pour windows, tag-count reconciliation) plus
   person↔plant proximity — not primarily haul-cycle corroboration.

### What is superseded in the existing vision docs

The `VISION_*.md` set is sound on the perception↔operational boundary, model-agnosticism,
licensing, and the edge/service split. The RAN Mines proposal changes these assumptions:

| Existing vision-doc assumption | RAN Mines correction |
|---|---|
| Vision follows the fuel/weighbridge quick wins (`PLATFORM_EVOLUTION` §7) | Vision is proposal **Phase 2**, *after* gate security + lab ingestion (Phase 1); reconciliation is Phase 3 |
| MVP = **haul-truck** detection + zone entry | MVP class set is unchanged technically, but the first *commercial* vision slices are **person counting / restricted-area / dwell** in fixed security zones (gold room, smelt house) |
| GPS/vision fusion (haul-cycle corroboration) is a primary early capability | Still valid, but **secondary** here; RAN Mines value is loss/security/reconciliation. `VISION_GPS_FUSION.md` applies to mobile-plant safety, not gold-room security |
| Cameras appear as `source: "vision:<cam>"` on `event.v1` | Unchanged and correct — but we now also need a **camera estate registry** (106 cameras, AI-suitability) that the vision docs did not cover |

These are re-prioritisations, not rewrites. No `VISION_*.md` content is deleted; the
feature plans below carry the corrected emphasis.

---

## 2. Current foundation (reuse — do not rebuild)

Confirmed in the repository (28 tables, 20 routers, 7 contracts). These are platform
primitives every new capability **reuses**:

- **Event spine:** `event.v1` (advisory, unified alarm queue, dedupe, ack), `events/`,
  SSE `stream.py`. Every new source emits into this — no second event framework.
- **Contract registry / bus / metrics:** `platform/{contracts,bus,metrics}.py`, `/api/v1`.
- **Geospatial:** `zones/` (polygons, `kind`, debounce/hysteresis), `asset_zone_state`.
  Camera image-zones map onto these operational zones — no second zone framework.
- **Identity & tenancy:** `sites`, `assets` (+`asset_class`), `operators` (PII in one
  table, FK-referenced), `users` + RBAC (`viewer<supervisor<admin`, `device`), `audit_log`.
- **Device lifecycle & credentials:** `devices/` (provisioning, `$7$` secrets, broker ACL).
  Cameras and vision-edge nodes reuse this as `device`-role principals.
- **Time model:** `shift_definitions`, cross-midnight shifts — reused for scheduled-pour
  windows and time-window association in reconciliation.
- **Annotations & workflow:** `incidents`, `incident_notes`, `delay_classifications`,
  `shift_handovers` — reconciliation exceptions and security events create incidents here.
- **Measured-data domains as precedent:** `weighbridge/` (`weigh_tickets`, idempotent,
  net-consistency **flagged not corrected**), `fuel/` (measured vs calculated, ratios
  `None` not fabricated), `maintenance/` (deterministic, `inferred`, `Unknown` when data
  is thin), `dispatch/` (recommend→approve→inform, advisory). **These four are the
  templates** for how the new domains behave.
- **Resilience & deployment:** store-and-forward spool, retention (per class), MinIO,
  Postgres/TimescaleDB, one-command on-prem `docker compose`, backup/restore.

---

## 3. Requirement → capability map

Legend: **E** = existing platform capability (reuse) · **N** = new Mine Monitor capability
to build · **X** = external system integration · **?** = unknown, needs Phase 0 / site
validation.

| Proposal requirement | Class | How |
|---|---|---|
| Unified alarm/event queue for all sources | **E** | `event.v1` + `events/` + SSE; extend `type` enum additively |
| Restricted-area detection | **E**+N | reuse `zones/` + `event.v1`; N = camera image-zone→zone mapping |
| Time-window / shift association | **E** | `shift_definitions`, cross-midnight logic |
| RBAC / audit / notifications / evidence store | **E** | `auth/`, `audit_log`, `notifications/`, MinIO |
| Camera estate + AI-readiness for 106 cameras | **N** | new camera registry (FP-01), device-model reuse |
| Vision edge runtime (stream→detect→track→observe) | **N** | FP-02; `VISION_*.md` architecture |
| `vision.observation.v1` / `vision.operational_event.v1` | **N** | specified in `VISION_EVENT_CONTRACTS.md`; register in `platform/contracts.py` |
| Area headcount (no facial recognition) | **N** | FP-03 |
| Person presence / entered / left / remained / off-schedule | **N** | FP-03/04, reuse zones + shifts |
| Two-person rule (count + evidence + confidence; no identity) | **N** | FP-04 |
| Lingering / dwell behaviour | **N** | FP-04, temporal reasoning |
| Scheduled-pour monitoring | **N**+X | FP-04; **?** scheduling data source |
| Tag correlation (count reconciliation, not identity) | **N**+X | FP-05; **?** tag system protocol/counts |
| Person↔mobile-plant proximity safety | **N** | FP-06; reuse safety `event.v1`; advisory |
| Camera health / AI suitability metadata | **N** | FP-01 |
| Gate: authorised identity, reject off-shift/suspended/non-inducted | **E**+X | FP-07; identity/decision logic on ingested access events |
| Metal-detector integration; random-search selection; missed-search escalation | **N**+X | FP-07; **?** detector interface |
| Face system at gate | **X** | ingest **access events only** — no biometric templates (CLAUDE.md §4) |
| Lab: ingest instrument output, remove retyping | **N**+X | FP-08; **?** instrument interfaces/formats |
| Lab: preserve original, hash/fingerprint, append-only corrections, actor identity | **N** | FP-08; provenance discipline |
| Lab: detect anomalous values | **N** | FP-08 (deterministic bounds first; models later) |
| Ingest weightometer / carbon / production / pour measurements | **N**+X | FP-09; **?** weightometer/scale data access |
| Gold reconciliation: expected vs observed, normal variation, unexplained variance → exception | **N** | FP-09; **flag, never conclude theft** |
| Cross-source loss-pathway correlation | **N** | FP-10; correlates events from all sources |
| Drones / conveyor / stockpile / stock-take (Phase 4) | **later** | keep architecturally possible; do not build now |

**Contracts:** `access.event.v1` (FP-07), `laboratory.result.v1` and
`laboratory.correction.v1` (FP-08) are **published and registered** — their slices have landed.
Still to add **only when their slice lands** (not speculatively): `vision.observation.v1`,
`vision.operational_event.v1` (specified), `material.measurement.v1`,
`reconciliation.exception.v1`. Security/safety vision events that need a human still ride the
**existing** `event.v1` (add `type` values additively — FP-08 added `lab_anomaly` and
`lab_result_conflict`).

---

## 4. Dependency map

```
                        ┌─────────────────────────── existing platform ───────────────────────────┐
                        │ event.v1 · zones · shifts · assets/operators · RBAC · audit · MinIO · SSE │
                        └───────────────────────────────────┬──────────────────────────────────────┘
                                                             │ (reused by all)
  FP-01 Camera estate & AI readiness ──┐
     (non-blocked; structures Phase 0)  │
                                        ▼
  FP-02 Vision edge platform ───► FP-03 Person counting ───► FP-05 Tag reconciliation
     │                              │      │                        ▲
     ├───► FP-06 Plant safety       │      └──► FP-04 Security behaviour (dwell, 2-person,
     │      (person↔plant)          │                restricted, scheduled) 
     │                              │                        │ scheduling data (?)
     ▼                              ▼                        ▼
   (all vision needs cameras audited: FP-01, and camera secondary AI streams: Phase 0)

  FP-07 Access integration (gate/turnstile/metal-detector/face) ──┐
     │ (external protocols ?)                                       │
  FP-08 Laboratory ingestion ──────────────────┐                   │
     │ (instrument interfaces ?)                ▼                   ▼
  (weightometer/production feeds ?) ──► FP-09 Gold reconciliation   │
                                                 │                  │
                                                 ▼                  ▼
                                       FP-10 Loss-pathway intelligence
                                   (correlates vision + access + lab + weightometer
                                    + production + telemetry events → exceptions)
```

Key dependencies:
- **FP-01 gates all vision** (02–06): you cannot deploy a detector to a camera you have
  not audited for an AI-suitable secondary stream.
- **FP-02 gates 03/04/06**; **FP-03 feeds 05** (vision count) and **04**.
- **FP-05** needs **FP-03** (vision count) + the tag system (X, Phase 0).
- **FP-09** needs **FP-08** (lab) + weightometer/production feeds (X, Phase 0).
- **FP-10** is last: it consumes structured events from vision, access, lab, weightometer,
  production, and telemetry — build it only once ≥2 of those sources exist.

---

## 5. Implementation roadmap (vertical slices)

Follows the proposal's slice order. Each slice is independently testable and deployable,
additive, migration-backed, and reuses platform primitives.

| Slice | Deliverable | Feature plan | Gating dependency |
|---|---|---|---|
| **1** | Camera inventory + AI-readiness registry (the Phase 0 audit tool) — **✅ DELIVERED** (migration 0019, `cameras/` domain, `/api/v1`, tests; PG16 round-trip) | FP-01 | none — **start here** |
| 2 | One camera → pretrained detector → tracked objects → `vision.observation.v1` | FP-02 | Phase 0 camera stream access |
| 3 | Person counting in one configured zone | FP-03 | slice 2 |
| 4 | Person↔plant proximity event | FP-06 | slice 2 |
| 5 | Restricted-area event | FP-04 | slice 2 + zones (E) |
| 6 | Dwell-time / temporal behaviour (+ two-person, scheduled) | FP-04 | slice 3; scheduling data (?) |
| 7 | Tag / headcount reconciliation | FP-05 | slice 3 + tag system (?) |
| 8 | Access-control integration (gate/turnstile/metal-detector/face events) | FP-07 | interfaces (?) |
| 9 | Laboratory ingestion (immutable original, hash, append-only corrections) | FP-08 | instrument formats (?) |
| 10 | Weightometer / material measurements | FP-09 | scale access (?) |
| 11 | Gold reconciliation engine | FP-09 | slices 9+10 + baseline (?) |
| 12 | Cross-source loss-pathway intelligence | FP-10 | ≥2 upstream sources |

Note: the proposal sequences slices 8–9 (gate + lab, its *Phase 1*) as commercially first,
but they are **externally blocked** on interfaces discovered in Phase 0. **Slice 1 is the
only fully non-blocked software slice** and it is the tool that runs the audit — hence the
recommendation in §9. Vision slices 2–7 unblock as camera streams are confirmed; gate/lab
slices unblock as their interfaces are characterised.

---

## 6. Phase 0 / site-information blockers

These must come from the Phase 0 audit (proposal: 2–3 weeks, cameras/gates/spreadsheets/
control-room/lab). Nothing downstream should pretend to be complete without them.

| Blocker | Blocks | Needed |
|---|---|---|
| Which of 106 cameras expose an AI-usable **secondary stream**; resolution/fps/codec; lighting; blind spots; zone coverage | all vision (02–06) | camera-by-camera audit → FP-01 registry |
| Gate / turnstile / metal-detector **interfaces & protocols** | FP-07 | vendor/API/protocol per device |
| Face/payroll system **event interface** (events only, no templates) | FP-07 | how to receive access decisions |
| Laboratory **instrument output** formats & interfaces | FP-08, FP-09 | file/serial/DB/LIMS per instrument |
| Crusher **weightometer** data access; carbon & production/pour data sources | FP-09 | protocol + units + sampling |
| **3–6 months of spreadsheets** for normal-variation baselines | FP-09 reconciliation tolerances | historical metallurgical data |
| **Tag system** spec (protocol, per-area active counts) | FP-05 | reader/API + area mapping |
| **Scheduling / pour** data source | FP-04 scheduled-pour, FP-06 | operational schedule feed |
| Edge **hardware** available per camera location (CPU/GPU) | vision deployment profile | site power/network/compute survey |

---

## 7. Feature-plan index

Created under [`docs/feature-plans/`](feature-plans/):

| # | File | Purpose |
|---|---|---|
| 01 | `CAMERA_ASSET_AND_AI_READINESS.md` | Manage the 106-camera estate; determine AI suitability |
| 02 | `VISION_EDGE_PLATFORM.md` | The Mine Monitor Vision edge runtime |
| 03 | `VISION_PERSON_COUNTING.md` | Area headcount & presence |
| 04 | `VISION_SECURITY_BEHAVIOUR.md` | Restricted-area, dwell, two-person, scheduled, abnormal presence |
| 05 | `VISION_TAG_RECONCILIATION.md` | Camera headcount vs tag count reconciliation |
| 06 | `VISION_PLANT_SAFETY.md` | Person↔mobile-plant proximity |
| 07 | `ACCESS_CONTROL_INTEGRATION.md` | Gate / turnstile / metal-detector / face event ingestion |
| 08 | `LABORATORY_DATA_INGESTION.md` | Instrument ingestion, provenance, immutable originals, corrections |
| 09 | `GOLD_RECONCILIATION.md` | Continuous process/material reconciliation |
| 10 | `LOSS_PATHWAY_INTELLIGENCE.md` | Cross-source correlated-discrepancy intelligence |
| 11 | `GENERATOR_MONITORING.md` | DSE generator fuel + run-hours (added 14 Sep, §0) |
| 12 | `VISION_DATASET_BOOTSTRAPPING.md` | DINOv3/DINOv2 frozen-backbone dataset bootstrapping (few-shot, retrieval, active-learning, anomaly) — dev/offline, licence-gated |

Assessments (in [`docs/assessments/`](assessments/)): `PHASE1_PERSONNEL_TRACKING_ASSESSMENT.md`,
`QUESTIONS_FOR_DEREK.md`, `RANMINES_CAMERA_AI_ASSESSMENT.md`.

---

## 8. What stays outside Mine Monitor

- **Facial recognition / biometric matching** — the vendor face terminal stays on the mine
  network; Mine Monitor ingests **access events only**, never templates or images
  (`CLAUDE.md` §4).
- **Raw video & recorders** — existing NVRs keep the primary stream; the vision edge
  consumes a **secondary** stream and keeps frames/clips local. Raw video is not
  centralised (bandwidth + data protection); only structured events + referenced evidence
  clips cross.
- **Proprietary tag hardware/readers** — Mine Monitor ingests counts/events, not the radio
  layer.
- **Laboratory instrument internals** — we ingest outputs; instruments stay the system of
  record for their raw signal.
- **Machine control of any kind** — advisory only (§10 below).
- **Phase 4** (drones, conveyor optimisation, stockpile/stock-take, road-train) — kept
  architecturally possible, not built now.

---

## 9. Recommended first engineering slice

**Slice 1 — Feature Plan 01: Camera estate & AI-readiness registry.**

Why this and not gate/lab (the proposal's commercial Phase 1):
- **It is the only fully non-blocked software slice.** Gate, lab, weightometer and tag work
  all wait on interfaces discovered during Phase 0; camera counting waits on stream access.
- **It is the tool that conducts Phase 0.** A camera registry with AI-suitability metadata
  (id, location, stream URL/type, resolution, fps, codec, lighting, usable-AI-stream,
  suitability, blind spot, zone mapping, calibration, model deployed/version, health) is
  exactly the structured output the 106-camera audit must produce.
- **It reuses existing primitives** (asset/device registry patterns, `/api/v1`, RBAC,
  audit, dashboard section) — low risk, additive, no external dependency.
- It immediately unblocks vision slices 2–3 (person counting in the gold room / smelt house
  is the first commercially-relevant vision capability) as soon as suitable streams are
  confirmed.

Concretely: one additive migration (`cameras` table + AI-readiness fields), a `cameras/`
domain + `/api/v1` CRUD (admin-write, viewer-read, audited), a dashboard section, and tests
— mirroring how `devices/` and `weighbridge/` were introduced. No model code, no inference,
no external integration.

---

## 10. Risks & invariants (carried from the platform, reaffirmed by the proposal)

| Risk | Mitigation |
|---|---|
| **Identity overreach** — naming individuals from a camera or a missing tag | Count reconciliation only; never "person X has no tag" without an independently validated identity association; `person`, never a name |
| **Fabricated measurements** where no sensor exists | Adapters only where the source exists; measured vs calculated vs inferred labelled; `Unknown`/`None` not guessed (fuel/maintenance precedent) |
| **Reconciliation false-positives** erode trust | Baseline normal variation from historical spreadsheets (Phase 0); **flag variance, never conclude theft**; human decides |
| **Raw-video centralisation** saturates the thin link | Edge-only processing; secondary stream; structured events + optional clip refs only |
| **Licence trap** | Apache/MIT/BSD only; Ultralytics/AGPL excluded; verify code **and** weights **and** dataset (`VISION_MODEL_LICENSES.md`) |
| **Cloud creep** into the operational core | On-prem/local-first; edge inference, local storage/events/alerting; cloud optional-later only |
| **Safety-boundary drift** | **Advisory only** (`event.v1.advisory === true`): warns people, never brakes/stops/locks/controls plant. A crossing requirement is escalated, not built (`CLAUDE.md` §15) |
| **Data protection** (people in gold room = personal data) | Counts not identity; per-site scope/export/delete; retention per class; audit on personal-data access (`CLAUDE.md` §4) |
| **Scope explosion** (12 slices, many external systems) | Strict slice order + STOP gates; smallest coherent increment; this doc + feature plans reviewed before building |

---

## 11. Provenance requirement (every external observation)

Mine Monitor must always answer: *what did we know, when, from where, what did the system
infer, and what did a human decide?* Each source carries provenance, matching the
platform's existing labelling (`observed | calculated | inferred | correlated | estimated |
unknown`):

- **Camera:** camera id · ts · model id/version · confidence · zone · track id · evidence ref
- **Laboratory:** instrument · ts · original file/result · hash/fingerprint · correction
  history · actor
- **Weightometer:** source · ts · measurement · quality/status
- **Access:** source system · credential/tag/card/face event · ts · gate · decision · reason

---

## 12. STOP

This completes the alignment pass. Deliverables produced: the architecture delta (§1),
requirement map (§3), dependency map (§4), roadmap (§5), Phase 0 blockers (§6), feature-plan
set (§7 + `docs/feature-plans/`), out-of-scope (§8), first slice (§9), and risks (§10).

No significant code changed. Implementation begins only on review, starting with Slice 1
(FP-01), and each subsequent slice unblocks as its Phase 0 dependency is satisfied.
