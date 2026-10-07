# Mine Monitor — Data-Lake & Intelligence-Layer Architecture

**Status: reference architecture, for review.** This formalises the target the RAN Mines team
described in the 21 Sep meeting — *"a centralized data lake aggregating cameras, UWB, drones,
X-ray and spectrometers → an intelligence layer that classifies alerts by priority
(high/medium/missed) into dashboards."* That target **is Mine Monitor**: this document maps each
named source onto the contract spine, storage tiers, intelligence layer and unified alarm queue
that already exist (or are specified) in this repo, rather than proposing a parallel system.

It complements `PLATFORM_EVOLUTION_ARCHITECTURE.md` (the platform's evolution rules),
`VISION_BUILD_GATE.md` (the camera/NVR path) and `RAN_MINES_PROPOSAL_ALIGNMENT.md` (the
integration/reconciliation framing). Nothing here relaxes the standing invariants: **advisory
only**, **no biometric templates**, **on-prem-capable**, **measured-vs-inferred provenance**,
**multi-tenant `site_id`**, **store-and-forward**.

---

## 1. What "data lake" means here (and what it does not)

Not a cloud warehouse (Databricks/Snowflake) and not schema-less object dumps. On a remote
Zimbabwean mine behind a single, congested Starlink link, the lake is a **self-hostable,
on-prem, contract-validated store** with three tiers — the same PostgreSQL 16 + TimescaleDB +
MinIO stack the platform already runs from one `docker compose up`:

| Tier | Contents | Store | Property |
|---|---|---|---|
| **Raw / landing** | every source's observations, exactly as ingested | Timescale **hypertables** (high-volume) + write-once record tables | **immutable**, append-only, schema-on-**write** (validated against a `*.v1` contract at the boundary) |
| **Curated / derived** | analytics recomputable from raw | ordinary tables | **reproducible** — a rule/model fix re-derives them; never hand-edited |
| **Serving / annotation** | the alarm queue, incidents, human notes, dashboards | ordinary tables | operator-facing; annotations kept **separate** from raw so history never rewrites |
| **Evidence** | short clips, frames, X-ray images, scans | **MinIO** (S3) | **referenced**, never streamed to cloud; per-class retention + deletion job |

The one rule that makes it a *lake* and not a swamp: **every source speaks a versioned event
contract, validated and rejected-loudly at the ingest boundary.** Anything that cannot be
expressed as a `*.v1` contract is a design smell — fix the contract, don't special-case it.

---

## 2. The five layers (L0–L4)

```
L0  SOURCES (site LAN / edge — nothing raw crosses the thin uplink)
    Cameras (Dahua DSS/NVR AI) · UWB tags · drones (DJI) · baggage X-ray ·
    spectrometer (lab) · GNSS trackers · gate/turnstile + metal detector ·
    weighbridge/weightometer · DSE generators · fuel · HR/roster export
        │  each via a protocol adapter → normalise → validate against a *.v1 contract
        ▼
L1  EDGE / INGEST  (store-and-forward; buffers local, backfills on reconnect)
    HTTP ingest · MQTT · TCP (trackers) · DSS/NVR poller · file/CSV import ·
    (later) vision edge service, UWB RTLS bridge, Modbus, drone bridge
        │  validated events + evidence refs
        ▼
L2  DATA LAKE / PLATFORM CORE  (on-prem; the tiers of §1)
    raw hypertables · derived tables · evidence (MinIO) · contract registry ·
    in-process event bus · retention · audit
        │
        ▼
L3  INTELLIGENCE LAYER  (rules & correlation — turns observations into meaning)
    zone/geofence engine · rules (restricted/overspeed/dwell/offline/occupancy) ·
    haul-cycle & metrics · fusion (UWB↔vision↔gate count reconciliation) ·
    gold reconciliation (expected-vs-observed) · dedup + severity/priority policy ·
    absence detection ("missed")
        │  produces event.v1 (advisory) into ONE queue
        ▼
L4  APPLICATIONS  (dashboards & action)
    unified alarm queue (priority: high / medium / missed) · incident workflow ·
    reports/exports · notifications (WhatsApp/webhook/email) · one-way cloud
    outbound sync for remote access
```

The **hard boundary** sits between L2 and L3: L0–L2 record *what was observed* (provenance
`observed`/`measured`/`vendor_inferred`); L3 asserts *what it means* (provenance
`inferred`/`correlated`/`estimated`), always with the evidence that produced it. This is what
lets a mine manager see whether "6 people in the gold room" was **measured** (UWB tag count),
**inferred** (vision), or **correlated** (both) — and never presents a guess as a fact.

---

## 3. The contract spine — every source, one shape

Alarms are always the existing `event.v1`; source records are per-domain `*.v1`. **Built** =
implemented and tested in this repo today; **specified** = designed, lands when its slice does.

| L0 source | Ingest path (L1) | Source contract | Status | → `event.v1` types |
|---|---|---|---|---|
| GNSS trackers | TCP/MQTT | `asset.position.v1` | **built** | `zone_breach·overspeed·zone_dwell·zone_occupancy·asset_offline·geofence_exit` |
| Cameras (Dahua **DSS/NVR** AI) | DSS/NVR poller | `vision.vendor_event.v1` | **built** (sim-first) | `zone_breach·zone_dwell` (`source: nvr:<cam>`, `provenance: vendor_inferred`) |
| Cameras (first-party perception) | vision edge service | `vision.observation.v1` / `vision.operational_event.v1` | specified (STOP-gated) | `proximity·restricted_area·…` |
| Gate / turnstile + metal detector | HTTP / gate poller | `access.event.v1` | **built** | `access_denied·metal_detected·search_missed·source_clock_skew` |
| **UWB tags (RTLS)** | RTLS bridge | `uwb.position.v1` *(propose)* | **specified** | feeds occupancy + count reconciliation; `zone_breach·restricted_area·tag_lost` |
| **Baggage X-ray** | vendor export/API | `xray.scan.v1` *(propose)* | **specified** | `xray_alert` (item flagged in bag) |
| **Drones (DJI)** | drone bridge | `drone.event.v1` *(propose)* / reuse `event.v1` | **specified** | intrusion snippet + advisory deploy recommendation |
| Spectrometer (lab) | SpectrAA CSV/watch-folder | `laboratory.result.v1` + `laboratory.correction.v1` | **built** | `lab_anomaly·lab_result_conflict` |
| Weighbridge / weightometer | REST/CSV/serial | `weighbridge.transaction.v1` · `material.measurement.v1` | **built** | reconciliation inputs |
| DSE generators | Modbus (sim-first) | `generator.reading.v1` | **built** | `generator_fault·generator_unexpected_run` |
| Fuel | manual/dispenser | `fuel.transaction.v1` | **built** | consumption/reconciliation inputs |
| Gold reconciliation | derived (L3) | `reconciliation.exception.v1` *(propose)* | **specified** | `reconciliation_exception` |
| HR / roster | Excel export → CSV import | (operators + shift defs) | **built** (identity model) | drives access authorisation rules |

Adding a source is: an L1 adapter + one `*.v1` contract (JSON Schema + Pydantic mirror,
registered in `platform/contracts.py`) + a storage table + tests. No new event framework, no
second queue. The three proposed contracts (`uwb.position.v1`, `xray.scan.v1`,
`drone.event.v1`) are **defined only when their slice lands**, per the platform's
no-speculative-contract rule.

---

## 4. The data model (lake tables by tier)

Every operational row carries `site_id` (multi-tenant) and timezone-aware UTC timestamps.

- **Raw / immutable (hypertables + write-once):** `positions`, `vision_vendor_events`,
  `laboratory_results`, `generator_readings`, `material_measurements`, `access_events`,
  `weigh_tickets`, `fuel_transactions`/`fuel_tank_readings` — plus, when their slices land,
  `uwb_positions`, `xray_scans`, `vision_observations`, `drone_events`.
- **Curated / derived (recomputable):** `haul_cycles`, `asset_metrics`, `asset_zone_state`,
  and (specified) `reconciliation_exceptions`, `count_reconciliations`.
- **Serving / annotation:** `events` (the one unified queue), `incidents` + `incident_notes`,
  `delay_classifications`, `shift_handovers`, `laboratory_corrections` (append-only).
- **Reference / config:** `sites`, `assets`, `operators` (the single PII home), `zones`,
  `shift_definitions`, `cameras`, `weighbridges`, `fuel_tanks`, `maintenance_plans`, `devices`,
  `users`.
- **Evidence (MinIO):** clip/frame/X-ray/scan blobs, referenced by URI from `event.evidence` —
  never inlined, never streamed to cloud.
- **Governance:** `audit_log` (append-only), `notifications` (store-and-forward outbox).

Idempotency is structural: every raw record has a **deterministic id** (e.g.
`nvr-<site>-<channel>-<id>`, `gen-<site>-<asset>-<ts>`, `mm-<site>-<stage>-<source>-<ref>`), so a
replay or backfill after a link outage never double-counts or double-alarms — essential on a
flaky link.

---

## 5. The intelligence layer — priority classification (high / medium / missed)

The client's three-way split maps cleanly onto the existing severity + a **third, absence-based
class** that most systems lack and that the mine explicitly asked for:

| Priority | Meaning | Mechanism (built today) | Examples |
|---|---|---|---|
| **High** | act now — safety or active security breach | `event.v1 severity=critical` | `access_denied` (unauthorised entry the gate let through) · `metal_detected` · `generator_fault` · unsafe proximity (vision, later) · restricted-zone entry |
| **Medium** | review — anomaly or policy signal | `event.v1 severity=warning` | `overspeed` · `zone_dwell` · `lab_anomaly` · `generator_unexpected_run` · reconciliation variance beyond tolerance |
| **Missed** | *expected-not-observed* — the coverage-gap class | absence detection over stored state | `search_missed` (selected search skipped) · `asset_offline` (tracker silent while active) · `camera_obstructed` (coverage lost) · `tag_lost` (UWB vanished with no gate exit) · an expected shift/pour measurement absent |

**Why "missed" is the differentiator.** The control room today drowns in 99+ live alerts and so
**ignores events entirely and visually scans** — and workers exploit the blind spots (creating
them when a PTZ locks on). A queue that only fires on *positive* detections can't help there.
Mine Monitor's value is (a) **dedup + severity grouping** so the queue is small enough to act on,
and (b) the **"missed" lane** that flags where the system *should* have seen something and
didn't — the exact gap workers exploit. Formalising `missed` as a first-class priority is the
concrete answer to "116 cameras, 2 operators, unmanageable."

**How classification works (deterministic, explainable):**
- **Dedup** — one open alarm per (subject, type) while the condition persists; a re-observation
  refreshes, it doesn't restack (already implemented for offline/occupancy/search).
- **Severity** — set by **rule/zone policy**, never by a model; a restricted zone → critical, a
  speed-limited zone → warning.
- **Correlation raises confidence** — two independent sources agreeing (UWB tag count == vision
  count in the gold room) is high-confidence; disagreeing is itself a signal (an untagged or
  masked person → high).
- **Evidence travels** — every derived alarm carries the contributing record ids, time window and
  per-signal contributions, so an operator sees *why* and *from what*.

### Cross-source correlations the layer enables (the commercial payload)
1. **Personnel count reconciliation** — UWB identified-count vs vision headcount vs gate
   first-in/last-out. A mismatch = an **untagged or masked** person in a controlled zone → high.
   (Gate FR keeps only first-in/last-out, so it cannot give continuous occupancy — UWB + vision
   fill that gap. State this plainly; don't overpromise gate data.)
2. **Gold-loss intelligence** — reconciliation variance (weightometer → mill → CIL → furnace →
   output → gate) **correlated with maintenance windows** (the named theft vector: gold sludge
   from CIL tanks during maintenance) + who was present (UWB/access) → an **exception with
   evidence**, never a theft verdict (a human concludes).
3. **Blasting headcount** — UWB count in a blast zone before detonation → a safety clearance
   check (currently manual).
4. **Drone dispatch** — an intrusion event (camera/UWB) → an **advisory** deploy recommendation;
   the platform recommends, a human/DJI executes. Nothing here actuates plant.

---

## 6. Connectivity, bandwidth & remote access (the binding constraint)

The uplink is a single Starlink (~300 Mbps, **congested**); the site LAN is strong (plant
cameras P2P 1 Gbps < 100 m, perimeter fibre to 2 km). The architecture is designed to that
asymmetry:

- **The lake lives on-prem.** All ingestion, storage and the intelligence layer run on a box at
  the mine (`docker compose up`), so a link outage never stops detection, alarms or analytics.
- **Only metadata + snippets cross the link.** Structured `event.v1` + `*.v1` records are small
  JSON; evidence is a *reference* to a clip/frame/scan retained on-prem (fetched on demand),
  never a continuous video stream. This is both the bandwidth design and the data-protection
  posture (frames may contain identifiable people).
- **One-way outbound sync** pushes events + evidence refs to a cloud mirror for remote
  monitoring (`PLATFORM_EVOLUTION_ARCHITECTURE.md`), ideally over a **dedicated Starlink for
  Eigenstate** so it isn't starved by site traffic. If remote connectivity can't be secured,
  on-site work is the fallback (noted in the minutes).
- **Store-and-forward everywhere** — every L1 adapter buffers locally and backfills in order on
  reconnect; idempotent ids make the backfill safe.

---

## 7. Integration surfaces confirmed (RAN Mines, 21 Sep)

- **Cameras → Dahua DSS** (not five separate NVRs). The DSS platform is being **upgraded (model
  TBD) and Evert must confirm integration compatibility before the order** — so the data-lake
  camera feed should target the **DSS API/SDK**, and we should be in that model decision to
  guarantee documented API access. Estate: ~116 cameras, ~5–6 models, **90% 8MP @ 25 fps on
  mainstream** (inference resolution is *not* the constraint), 16 with facial recognition (we
  ingest **events only, never templates**), 60 TB continuous recording across 5 NVRs.
- **UWB** is the identified-tracking source (10 cm), entry-on/exit-off — **personal data**, so it
  lands behind the `operators` foreign key like all identity, and its use is **gated on the
  Zimbabwe HR/NEC legality check** (frame as H&S/blasting headcount).
- **X-ray** baggage scanner (arriving) at mine exit + visa entry — a new event source.
- **HR/roster** exports to Excel today → a CSV import path (like weighbridge/lab) feeds the
  access-authorisation rules.

---

## 8. Governance, safety & data protection (non-negotiable)

- **Advisory only.** Every output is `event.v1.advisory === true`. The lake informs and
  recommends (including drone deployment); it never brakes, stops, or actuates plant.
- **No biometric templates, ever** (`CLAUDE.md §4`). Cameras and the gate FR system yield only
  *events*; face images/templates stay in the vendor appliance. UWB and operator identity live in
  the one `operators` table (exportable/erasable per person), referenced by foreign key — never a
  name in an event payload.
- **Provenance on every field** — `observed·measured·vendor_inferred·inferred·correlated·
  estimated·unknown` — rendered on the dashboard; a guess is never shown as a measurement.
- **Retention per data class** with a scheduled deletion job; **audit log** on personal-data
  access and rule/zone changes; **RBAC** (`viewer<supervisor<admin`, `device`); **multi-tenant**
  `site_id` scoping on every query (this is mine #1, not mine only).

---

## 9. Build status & sequence

- **Built and merged / in review today:** the L2 lake tiers, the contract spine, the unified
  `event.v1` queue with dedup + severity + incident workflow, and ingestion for GNSS, gate
  (access, random search, metal detector), lab (spectrometer), weighbridge, fuel, dispatch,
  cameras (NVR vendor-event Stage A), generators (DSE), and gold-reconciliation measurement
  groundwork. Occupancy config + dashboard surface. One-way cloud sync specified.
- **Specified, slice-gated:** first-party vision perception (behind the `VISION_BUILD_GATE`
  review), UWB / X-ray / drone contracts + adapters, reconciliation exceptions (need weightometer
  feeds + baseline data), count-reconciliation fusion.
- **Phase order (matches the client's agreed phasing):** Phase 1 = camera + entry security on the
  spine; Phase 2 = central join (the lake + intelligence layer correlating across sources);
  reconciliation and drones follow as their feeds/gates clear.

## 10. Open questions (resolve, don't guess)

- **DSS**: chosen model + documented API/SDK for AI-event + snippet export; auth; poll vs
  subscribe. (Blocks the live camera feed — be in the model decision.)
- **UWB**: vendor/protocol (RTLS bridge shape), and the **HR/NEC legality answer** before UWB
  enters the proposal.
- **X-ray**: model + export interface (event/API vs file).
- **Drone**: DJI integration surface for autonomous-deploy-on-intrusion and remote flight.
- **Connectivity**: dedicated-Starlink decision (enables remote implementation vs on-site).
- **Baseline data**: 3–6 months of production spreadsheets to set reconciliation tolerances.
```
