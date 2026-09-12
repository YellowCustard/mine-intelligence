# Mine Monitor — Platform Evolution Architecture (Phase 0)

**Status: architecture discovery for review. No Phase 1+ implementation has begun.**

This document maps the current production system as it actually exists in the
repository, proposes a target architecture for the nine advanced capabilities
(computer vision, predictive maintenance, fuel, weighbridge, dispatch, mobile,
microservices, Kubernetes, cloud), and defines a migration strategy that keeps the
existing on-premise platform operating throughout. It is the artifact to review
before Phase 1 begins.

Guiding constraint, restated because it governs every decision below:

> The mine must operate the core platform **without public internet, without cloud,
> and without Kubernetes.** Cloud and Kubernetes are options, never requirements.

---

## 1. Current architecture map

One Python service image (`api`, `ingestor`, `teltonika`, `simulator` are the same
image with different entrypoints), one PostgreSQL 16 + TimescaleDB database, one
Mosquitto broker, MinIO, and an optional Caddy TLS proxy — all via one
`docker compose`. 14 Alembic migrations; 20 tables; 3 published event contracts.

| Component | Responsibility | Technology | Data owned | Inputs | Outputs | Depends on |
|---|---|---|---|---|---|---|
| **Ingest — Teltonika** (`ingest/adapters/teltonika.py`) | Decode Codec 8/8E over raw TCP; IMEI→device authz; republish normalised positions | asyncio TCP | none (stateless) | tracker TCP frames | `asset.position.v1` → MQTT | devices table (IMEI resolve), MQTT publisher |
| **Ingest — MQTT** (`ingest/mqtt.py`) | Subscribe to position topics; idempotent durable store; anti-spoof + strict device gate | paho-mqtt, SQLAlchemy | writes `positions` | MQTT `mm/<site>/<asset>/position` | stored positions; triggers pipeline | broker, DB, devices |
| **Ingest — HTTP** (`api/routers/ingest.py`) | Same store path over REST | FastAPI | writes `positions` | `POST /ingest/positions` | stored positions | DB |
| **Publisher spool** (`ingest/spool.py`) | Crash-safe store-and-forward buffer | SQLite | local spool file | positions to send | MQTT delivery | filesystem |
| **Pipeline** (`pipeline.py`) | Per-position: zones → debounce → rules → events | pure Python + SQLAlchemy | writes `events`, `asset_zone_state` | a stored position | `event.v1` rows, zone transitions | zones, rules |
| **Zones** (`zones/`) | Polygon store, point-in-polygon, debounce/hysteresis | pure geometry | `zones`, `asset_zone_state` | positions | inside/entry/exit | — |
| **Rules** (`rules/`) | Restricted/overspeed/dwell/offline → events (data-driven) | pure Python | none (reads zone rule payloads) | positions, zone state | events | zones |
| **Cycles** (`cycles/`) | Haul-cycle state machine, queue time, per-shift rollups | pure Python | `haul_cycles`, `asset_metrics` | positions (recompute) | cycle/metric rows | positions, shifts |
| **Operations** (`operations/`) | shifts, equipment state, scorecard, exceptions, incidents, delays, handover, reports, trends, bottlenecks, data-quality, system-health | pure Python + SQLAlchemy | `incidents`, `incident_notes`, `delay_classifications`, `shift_handovers`, `shift_definitions` | positions, cycles, events, annotations | derived views, annotations | DB, cycles, events |
| **Events** (`events/`) | Unified alarm queue, dedupe, ack; enqueue notifications | SQLAlchemy | `events` | rule/offline output | alarm rows, notification enqueue | notifications |
| **Notifications** (`notifications/`) | Store-and-forward outbox; webhook/SMTP dispatch with backoff | urllib/smtplib | `notifications` | qualifying events | outbound alerts | DB, network |
| **Devices** (`devices/`) | Provisioning, enable/disable, secret rotation, broker ACL/passwd render | SQLAlchemy + PBKDF2 | `devices` | admin API | device rows, broker files | DB |
| **Auth** (`auth/`) | HTTP Basic, PBKDF2, lockout, verify cache, RBAC deps | SQLAlchemy | `users`, `auth_lockout` | credentials | authenticated principal | DB |
| **Storage** (`storage/`) | ORM models, engine/session, retention, heartbeat | SQLAlchemy 2.x | all tables | — | sessions | DB |
| **API** (`api/`) | FastAPI routers + SSE stream + middleware (security headers, request-id, error boundary) | FastAPI/uvicorn | none | HTTP | JSON, SSE, dashboard | all domains |
| **Dashboard** (`web/mine.html`) | Command Centre: live map, exceptions, analytics, handover, reports, replay, admin; role-aware nav | framework-free HTML + SSE | none (browser) | REST + `EventSource` | operator UI | API |
| **Background jobs** (ingestor maintenance loop, `_offline_loop`) | Offline detection, cycle/metric recompute, notification dispatch, retention — all on one periodic tick | threads | writes derived + annotations | timer | events, metrics, deletions | DB |
| **Config** (`config.py`) | 12-factor env settings + prod fail-fast guard | pydantic-settings | none | env | Settings | — |
| **Retention** (`retention.py`) | Per-class scheduled deletion, audited | SQLAlchemy | deletes by policy | timer | pruned rows + audit | DB |

**Data owned (20 tables):** `sites`, `assets`, `operators`, `zones`,
`shift_definitions`, `positions` (hypertable), `events`, `asset_zone_state`,
`haul_cycles`, `asset_metrics` (hypertable), `users`, `auth_lockout`,
`service_heartbeat`, `incidents`, `incident_notes`, `delay_classifications`,
`shift_handovers`, `audit_log`, `devices`, `notifications`.

**Event contracts (`contracts/`):** `asset.position.v1`, `event.v1`,
`asset.metrics.v1` — published as JSON Schema, validated at the ingest boundary,
mirrored as Pydantic models.

### Coupling, sync/async, and shared-DB observations

- **Coupling is by direct function call and a shared database**, not messages. The
  only real message bus today is MQTT, and only for **position ingest** (device →
  ingestor). Everything after a stored position — pipeline, rules, events,
  notifications, analytics — runs in-process against the shared schema.
- **Synchronous:** HTTP ingest → store → pipeline → events (one transaction). API
  reads. Incident/delay/handover writes.
- **Asynchronous / periodic:** MQTT ingest (broker-buffered), the ingestor
  maintenance loop (offline detection, recompute, notification dispatch, retention),
  the publisher spool drain, SSE polling.
- **Shared-DB dependency:** every domain reads and writes one Postgres schema. This
  is the single biggest fact governing service extraction — a service boundary that
  cuts across a foreign key (e.g. `incidents.event_id → events`,
  `notifications.event_id → events`, `incident_notes → incidents`) cannot be a
  clean data-ownership split without first breaking that FK into an event/lookup.

---

## 2. Target architecture (per capability)

The unifying idea: **extend the event model and add domain modules that publish
`*.v1` events; extract a module into a service only when a real forcing function
(edge isolation, different runtime, independent scaling) demands it.** The operational
core stays a single deployable that runs offline.

### 2.1 Computer vision — the one capability that forces an edge service now
Vision genuinely needs a different runtime (GPU, OpenCV/inference, high I/O near the
camera) and must not ship raw video to the core or the cloud. So it is the first
justified **separate edge process/service**, not an in-core module.

```
Camera (RTSP) → Edge Vision node (own process, GPU/CPU)
  frame decode → detection → tracking → spatial/temporal reasoning
  → structured `vision.observation.v1` / `vision.operational_event.v1`
  → published into the SAME event layer (MQTT topic, e.g. mm/<site>/vision/<cam>)
  → core ingestor maps them into event.v1 / new domain tables
Raw video + clips stay on the edge node; only structured events + optional clip refs cross.
```
Reuses the existing ingest contract discipline. Vision events carry
`observed|inferred|correlated|estimated|unknown` + confidence + `model_version` +
`correlation_id` + optional `clip_uri` (MinIO). GNSS/vision fusion is a
**correlation** module in the core, never a silent identity claim.

### 2.2 Predictive maintenance — in-core module first
Starts as `maintenance/` reading existing telemetry (operating hours from ignition +
motion, utilisation from `asset_metrics`, event history). Deterministic **health
indicators** before any model. New tables: `maintenance_components`, `service_schedules`,
`work_orders`, `faults`, `health_scores`. Every risk output carries
`Normal|Watch|Elevated|High|Critical|Unknown` + evidence + confidence. CAN/J1939/engine
telemetry enters through an **adapter interface** (like `DeviceAdapter`) only where the
sensor actually exists — never fabricated. Extract to a service only if model inference
becomes heavy.

### 2.3 Fuel — in-core domain module
`fuel/` with `fuel.transaction.v1` and `fuel.anomaly.v1`. **Measured** transactions
and tank levels first (adapters for dispenser/tank/manual); **calculated** efficiency
(l/h, l/km, l/tonne — the last needs weighbridge) explicitly labelled; anomaly
detection last, evidence-based. New tables: `fuel_stations`, `fuel_tanks`,
`fuel_transactions`, `tank_readings`.

### 2.4 Weighbridge — in-core domain + adapter framework
`weighbridge/` with a manufacturer-neutral `weighbridge.transaction.v1` and a
configurable adapter (REST/DB/CSV/TCP/serial). New tables: `weighbridge_sites`,
`weigh_tickets`. Reconciliation (dispatch vs weighbridge vs stock) **flags**
discrepancies, never auto-corrects.

### 2.5 Dispatch — decision-support only, in-core
`dispatch/` producing `dispatch.recommendation.v1` from production demand + equipment
state + queue/loader/dump state. **AI recommends → supervisor approves → system
dispatches information.** New tables: `dispatch_jobs`, `dispatch_assignments`,
`dispatch_recommendations`, `haul_routes`. Hard safety boundary preserved: **the
platform never actuates machinery** (the existing `advisory:true` invariant extends to
dispatch).

### 2.6 Mobile — new client against a versioned API, not a new backend
Offline-first field app (supervisor/dispatcher/maintenance/field). Needs: an
**API version prefix** (`/api/v1`), a **sync endpoint** with change tokens and
idempotency keys, and conflict handling (last-writer-wins on annotations, never on
raw telemetry). No new operational core — it consumes existing + new domain APIs.

### 2.7 Microservices — bounded contexts, extract on evidence
Bounded contexts identified from the code (see §3). Extraction order driven by real
forcing functions: **vision (edge/GPU) → notifications (independent, already
event-driven) → analytics/reporting (read-heavy, independent scaling)**. Identity,
telemetry, operations stay together (dense FK coupling).

### 2.8 Kubernetes — optional, after boundaries stabilise
`deploy/kubernetes/` mirrors compose once services are stable. Compose remains the
supported mine deployment. K8s is for multi-node/cloud scaling, not single-box mine ops.

### 2.9 Cloud — optional one-way sync, never a runtime dependency
An **outbound sync agent** (extends the notification-outbox pattern: durable queue,
backoff, idempotent) replicates selected data to a cloud warehouse for cross-site
analytics, model training, and backup replication. The mine core has **no read path
to the cloud** in the operational hot path; loss of cloud is invisible to operations.

---

## 3. Boundary analysis

| Existing module | Verdict | Rationale |
|---|---|---|
| auth / users / operators (identity) | **Remain together** | Cross-cutting; RBAC + PII home; every domain depends on it |
| sites / assets / devices | **Remain together (shared reference)** | Referenced by everything; the tenancy + identity backbone |
| positions / ingest / spool | **Internal module now; telemetry-service candidate later** | High volume; clean contract already; could scale independently |
| zones / rules / pipeline / cycles / events | **Remain together (operations core)** | Dense coupling; one transaction; the commercial heart |
| operations/* (scorecard…handover) | **Internal modules; analytics/reporting-service candidate** | Read-heavy, reproducible; extractable behind read APIs |
| notifications | **Eventually a service** | Already event-driven + durable queue; independent runtime; clean seam |
| **vision (new)** | **Edge service from day one** | GPU/video runtime; must not centralise raw video |
| maintenance / fuel / weighbridge / dispatch (new) | **Internal domain modules first** | Prove the domain + data before distributing; extract on evidence |
| mobile | **New client (edge/handset)** | Consumes versioned API; offline-first |
| reporting | **Internal; reporting-service candidate** | Independent, read-only |

Data that **must stay local to the mine:** raw telemetry, raw video/frames, live
operational state, credentials/secrets, PII (`operators`), audit log. Data
**suitable for optional cloud sync:** derived shift metrics, scorecards, trends,
incident/delay summaries (PII-referenced by id), model artifacts, backups.

---

## 4. Data architecture

- **Authoritative (immutable) machine observations:** `positions`, `weigh_tickets`,
  `fuel_transactions`, raw `vision.observation.v1`, engine/CAN samples. Write-once.
- **Derived (reproducible) data:** `haul_cycles`, `asset_metrics`, equipment state,
  scorecards, trends, health/risk scores, dispatch recommendations, bottleneck
  observations. Recomputable from source; never hand-edited.
- **Operational annotations (human judgement):** `incidents`, `incident_notes`,
  `delay_classifications`, `shift_handovers`, work orders, dispatch approvals. Stored
  separately from telemetry; link by id; never mutate the observed record.
- **Event streams (versioned):** existing `asset.position.v1`, `event.v1`,
  `asset.metrics.v1`; new `asset.state.v1`, `haul.cycle.v1`, `queue.event.v1`,
  `vision.observation.v1`, `vision.operational_event.v1`, `maintenance.fault/health/risk.v1`,
  `fuel.transaction/anomaly.v1`, `weighbridge.transaction.v1`,
  `dispatch.job/recommendation/assignment.v1`, `incident.v1`. Every event carries
  site, asset (where applicable), timestamp, source, `correlation_id`, schema version,
  and provenance/confidence where inferred.
- **ML data:** feature snapshots + labels for training, exported to cloud; **model
  registry** (id, version, dataset ref, status, confidence) in-core so predictions
  stay distinguishable from measured facts and models roll back, never silently swap.
- **Video/evidence:** frames/clips on the edge node + MinIO; only refs + structured
  events cross into the core. Retention configurable per data class (extends the
  existing retention job).
- **Cloud-syncable:** the derived + annotation-summary subset above, one-way.

**Invariant preserved throughout:** raw telemetry immutable; derived data
reproducible; annotations distinguishable from observations; every inferred value
labelled and evidenced.

---

## 5. Migration plan (incremental, production-preserving)

No big-bang. Every step is additive, migration-backed, tested, and reversible.

1. **Foundation (in-core):** add `/api/v1` prefix alongside current routes
   (compat shim); formalise an internal event-contracts package + a lightweight
   in-process event bus abstraction over the existing calls; extend observability.
   *No behaviour change.*
2. **Vision (edge):** stand up the edge vision node as a new process publishing
   `vision.*.v1` into MQTT; core ingests into new vision tables + `event.v1`. One
   camera, one zone, one class, one event — then expand. Core unaffected if the edge
   node is offline.
3. **Fuel → Weighbridge → Maintenance → Dispatch:** each a new in-core domain module
   + migration + contracts + API + UI section + tests, in that order (measured data
   before anomalies before predictions before optimisation).
4. **Mobile:** against `/api/v1` + a sync endpoint; offline-first.
5. **Service extraction (evidence-driven):** notifications, then reporting/analytics,
   then telemetry — each behind its existing API/contract, strangler-style.
6. **Kubernetes:** package the stabilised services; compose stays first-class.
7. **Cloud:** outbound sync agent for the syncable subset; never a runtime dependency.

Each migration ships with: rollback plan, Alembic up/down (round-tripped on real
PG16), compatibility strategy, tests (unit/integration/failure/security), docs,
deploy instructions.

---

## 6. Risk register

| Risk | Area | Severity | Mitigation |
|---|---|---|---|
| Service split cuts live FKs (`incidents/notifications → events`) → distributed joins, lost integrity | Architecture | High | Keep operations core together; extract only clean seams (notifications, reporting); replace FK with event+lookup before any split |
| Vision raw-video centralisation saturates thin mine link | Data/network | High | Edge-only processing; structured events + optional clip refs only; configurable video retention |
| Inferred values (vision/maintenance/fuel) presented as fact | Data trust | High | Mandatory `observed/inferred/correlated/estimated/unknown` + confidence + evidence on every derived output; UI must label |
| Cloud/K8s creep into the operational hot path | Operational | High | Hard rule: core runs offline; cloud sync one-way outbound only; K8s optional; enforce in tests (offline-mode e2e) |
| Fabricated sensor/ML capability without hardware/data | ML/hardware | High | Adapters only where the sensor exists; no predictive model until real history; deterministic indicators first |
| Dispatch drifting toward machine control | Safety | Critical | Preserve `advisory:true`; recommend→approve→inform only; no actuation path, ever |
| Mobile sync conflicts corrupt annotations or telemetry | Data | Medium | Raw telemetry never written by mobile; annotations last-writer-wins with audit; idempotency keys |
| Secret/credential sprawl across new services | Security | High | Reuse device/broker credential model; per-service auth; least privilege; nothing internet-exposed |
| Schema growth destabilises the shared DB | Deployment | Medium | Additive migrations, round-trip tested; per-domain table ownership; retention extended per class |
| Scope explosion (nine areas at once) | Delivery | High | Strict phase order + STOP gates; smallest coherent increment; this doc reviewed before Phase 1 |

---

## 7. Recommended implementation sequence

Ranked by business value ÷ (risk × dependency × hardware/data availability):

1. **Phase 1 foundation** — API versioning, contracts package, observability. *No
   hardware, unblocks everything, low risk.* **Do first.**
2. **Fuel (measured + reconciliation)** — high commercial value (theft/cost), modest
   hardware (dispenser/tank adapters or manual entry), no ML. Strong early win.
3. **Weighbridge** — high value (production/tonnes → enables l/tonne and dispatch
   targets), one adapter, deterministic. Often already has a data source on site.
4. **Computer vision (edge, minimal slice)** — high value + safety, but needs
   cameras + edge hardware; start with one camera/zone/class/event.
5. **Predictive maintenance (deterministic indicators)** — value grows with data;
   start rule-based, defer models until history exists.
6. **Dispatch (decision-support)** — depends on fuel/weighbridge/production signals
   being present; high value once they are.
7. **Mobile** — depends on `/api/v1` + sync; high field value.
8. **Service extraction → 9. Kubernetes → 10. Cloud** — only after boundaries and
   operational evidence justify them.

Value/dependency note: fuel and weighbridge rank above vision *for sequencing* only
because they are lower-risk, hardware-light wins that also feed dispatch; vision is
the highest strategic capability and the one that legitimately introduces the first
service, so its architecture is settled here even though its build follows the quick
wins.

---

## 8. STOP

This completes Phase 0. **No Phase 1+ implementation has been started.** The existing
production baseline (PR #16 branch) is untouched by this document — it adds only
`docs/PLATFORM_EVOLUTION_ARCHITECTURE.md`.

Review gates before proceeding:
- Confirm the boundary verdicts in §3 (especially what stays in the operations core).
- Confirm the sequence in §7 (fuel/weighbridge-first vs vision-first is the main call).
- Confirm the offline-core / one-way-cloud / advisory-only invariants as non-negotiable.

On approval, Phase 1 (foundation) is the first increment — additive and behaviour-
preserving — followed by the chosen domain in §7.
