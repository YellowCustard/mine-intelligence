# Mine Monitor — build brief

Project context for Claude Code. Read this fully before writing anything.

---

## 1. What this is

**eigenstate** (eigenstatesystems.com) has signed its first mine contract: a gold mine in
**Zimbabwe**. This repo is the platform we deliver against it.

The mine has been shown a demo covering four things: a live geofenced site plan, fleet telemetry,
AI camera detections, and mineral exploration targeting. That demo ran entirely on synthetic data.
This repo is where it becomes real.

**One developer builds this.** Every decision below is biased toward what one person can build,
operate and debug at 2am from another country. Prefer boring, well-documented technology over
clever technology. Prefer one language and one database over the "right" polyglot architecture.

---

## 2. What we are building in Phase 1 (this repo, right now)

**Telemetry, geofencing and the alarm spine.** No AI, no cameras, no machine learning.

This is deliberate. Two thirds of what was demoed needs no AI at all, and the AI parts are blocked
on site footage that does not exist yet. So we build the parts that are not blocked, and we build
them so the AI parts plug in later without a rewrite.

Phase 1 delivers:

1. GNSS trackers on mine machines reporting position and speed.
2. Geofenced zones with per-zone rules (restricted, loading, unloading, speed-limited).
3. Automatic alerts when a rule is breached, in one unified alarm queue.
4. Haul-cycle analytics derived from zone transitions — cycle time, and **queue time at the face**.
5. A live operations dashboard, which already exists as HTML and needs wiring to real data.

Item 4 is the commercial centre of gravity. "Trucks spend N% of the shift queueing" is the number
that justifies the system to a mine manager, and it falls out of geofence transitions for free —
no extra hardware, no models.

### Explicit non-goals for Phase 1

Do not build these. They are later phases or other repos, and building them now will sink the
project.

| Not now | Why |
|---|---|
| Computer vision / detection models | Blocked on site footage that does not exist. Separate repo (`flockvision`). |
| Facial recognition / face matching | Bought as a commercial gate terminal, not built. We only ingest its events. |
| Payload in tonnes | Requires OEM onboard weighing. A GNSS tracker cannot produce it. We report **loads counted**. |
| Fuel level / burn rate | Requires a CAN/J1939 tap or fuel sender. Out until hardware is confirmed on site. |
| Exploration / prospectivity | Already built and working in a separate `mpm` project. Runs offline on assay data. |
| Mobile apps | Web dashboard is responsive. Revisit after the mine actually uses it. |
| Multi-cloud, Kubernetes, microservices | One developer. One service, one database, one container host. |

---

## 3. Hard constraints from the environment

These are not preferences. Design to them from the first commit.

- **The network will fail.** Remote Zimbabwean mine site. Everything ingest-side must be
  store-and-forward: buffer locally, backfill on reconnect, accept out-of-order and late-arriving
  data without corrupting derived analytics.
- **Power will fail.** Assume unclean shutdowns. No in-memory-only state that matters. Every write
  path must be crash-safe.
- **Bandwidth is expensive and thin.** Positions are small JSON. Never stream video to the cloud.
- **The deployment may have to be on-premise.** Data-protection posture (below) may force the whole
  stack onto a box at the mine. Do not use managed cloud services that cannot be self-hosted.
  Everything must run from one `docker compose up`.
- **Timezone is Africa/Harare (UTC+2).** Store UTC, render local. Shifts cross midnight — never
  assume a day boundary equals a shift boundary.
- **Multi-tenant from day one.** This is mine number one, not mine only. Every table that holds
  operational data carries a `site_id`, and every query is scoped by it.

---

## 4. Data protection (read before designing any schema)

Zimbabwe's Cyber and Data Protection Act, and **SI 155 of 2024** made under it, apply here.
Reported position: biometric data is explicitly defined to include facial-recognition features;
processing sensitive personal data requires explicit written consent; data controllers must
register with POTRAZ before processing; breach notification is reportedly 24 hours to the
regulator and 72 hours to affected individuals.

Engineering consequences, which are non-negotiable:

- **The mine is the data controller. eigenstate is the processor.** Build like a processor:
  everything scoped, exportable and deletable per site.
- **No biometric templates in this database, ever.** When the face terminal is integrated, we ingest
  only an access event: `{asset_or_person_ref, gate_id, timestamp, granted|denied}`. Face images and
  templates stay inside the vendor appliance on the mine's own network. Do not design a schema that
  could hold a face template "for convenience".
- **Operator identity is a foreign key, never a name in an event payload.** Personal data lives in
  one table so it can be exported or erased on request without rewriting history.
- **Retention is configurable per data class** — raw positions, derived metrics, events, evidence —
  with a scheduled deletion job that actually runs. Write it in Phase 1, even if retention is set
  generously. Retrofitting deletion is miserable.
- **Audit log** on access to personal data and on rule/zone changes.

This is not legal advice and is not the final word — a Zimbabwean lawyer is confirming the position.
Build so that a stricter answer costs us configuration, not architecture.

---

## 5. Architecture

Five layers, mirroring the pattern already used in the poultry vision project so the two systems
share contracts and operational habits.

```
L0  Devices        GNSS trackers on machines · (later: cameras, face terminal at the gate)
L1  Edge/ingest    Protocol adapters → normalise → store-and-forward buffer
L2  Platform core  Ingest gateway · zone engine · rules engine · cycle analytics · storage · API
L3  Applications   Operations dashboard · alarm queue · reports · exports
L4  Learning loop  (Phase 2+, in the vision repo — not here)
```

The rule that keeps this extensible: **every source speaks the same event contract.** A geofence
breach, a camera detection and a gate access all arrive as `event.v1` and land in the same alarm
queue. The control room does not care which sensor saw it. Anything that cannot be expressed as
`event.v1` or `asset.metrics.v1` is a design smell — fix the contract, do not special-case it.

---

## 6. Data contracts

These are the spine. Version them, publish them as JSON Schema in `contracts/`, and validate at the
ingest boundary. The vision repo already emits an `event.v1`-shaped record; keep them compatible so
a camera node can publish into this platform with no translation layer.

### `asset.position.v1` — raw telemetry, high volume

```json
{
  "schema": "asset.position.v1",
  "site_id": "kn-zw-01",
  "asset_id": "HT-102",
  "ts": "2026-09-05T11:42:07Z",
  "lat": -17.8252,
  "lon": 31.0335,
  "altitude_m": 1483.2,
  "speed_kph": 47.0,
  "heading_deg": 118,
  "hdop": 0.9,
  "satellites": 11,
  "ignition": true,
  "source": "teltonika:fmb920",
  "received_at": "2026-09-05T11:42:11Z"
}
```

`ts` is device time, `received_at` is server time. Both are required — the gap between them is how
we detect and correct for buffered backfill.

### `event.v1` — anything worth a human's attention

```json
{
  "schema": "event.v1",
  "event_id": "01J9Z8...",
  "site_id": "kn-zw-01",
  "ts": "2026-09-05T11:42:07Z",
  "type": "zone_breach",
  "severity": "critical",
  "asset_id": "LV-07",
  "zone_id": "r1-explosives-magazine",
  "source": "gnss_geofence",
  "summary": "LV-07 entered R1 Explosives Magazine without authorisation",
  "detail": { "dwell_s": 34, "speed_kph": 12.0 },
  "evidence": { "positions": ["..."], "clip_uri": null },
  "advisory": true,
  "state": "open",
  "acknowledged_by": null,
  "acknowledged_at": null
}
```

- `type` — `zone_breach` · `overspeed` · `zone_dwell` · `asset_offline` · `geofence_exit`
  (later, from other sources: `proximity` · `bog_precursor` · `belt_state` · `access_granted` ·
  `access_denied`)
- `source` — names the sensing modality (`gnss_geofence`, `vision:cam-01`, `access:gate-1`). The
  dashboard groups by severity, not by source.
- `advisory` — always `true` in this system. **The platform warns people. It never actuates plant.**
  Keep this field in the payload so the distinction travels with the data rather than living in a
  slide. Taking control of a machine is a different product with a certification burden measured in
  years; nothing in this repo may cross that line.

### `asset.metrics.v1` — rollups that drive the dashboard

Per asset per 5-minute bucket: distance travelled, moving time, idle time, max/mean speed, zone
dwell breakdown, loads completed. Derived, recomputable from positions, never hand-edited.

---

## 7. Technology decisions (already made — do not relitigate)

| Layer | Choice | Reason |
|---|---|---|
| Language | **Python 3.11+** | The vision pipeline is already Python. One developer should not run two ecosystems. |
| API | **FastAPI** + Pydantic v2 | Pydantic already used in the vision repo; the contracts above become models directly. |
| DB | **PostgreSQL 16 + TimescaleDB** | One database. Hypertables for positions/metrics, ordinary tables for everything else. Avoids running Postgres *and* ClickHouse. |
| ORM/migrations | SQLAlchemy 2.x + Alembic | Boring and well documented. |
| Device transport | **MQTT (Mosquitto)** for anything that can speak it; raw **TCP** listener for trackers that cannot | Trackers mostly speak their own binary TCP protocol. Adapters normalise into MQTT internally. |
| Live UI updates | **Server-Sent Events** | One-way server→browser. Simpler than WebSockets and survives flaky links better. |
| Object storage | MinIO (S3 API) | Evidence clips later. Self-hostable. |
| Packaging | **uv** | Fast, lockfile, reproducible. |
| Lint/format | ruff | One tool. |
| Tests | pytest | |
| Deploy | docker compose | Must run entirely on one box at the mine if required. |

### Frontend: extend, do not rewrite

A complete operations dashboard already exists as a **single self-contained HTML file** with an
inline-SVG site plan, geofenced zone rendering, asset markers, route trails, an alarm table and a
cycle-time chart. It is good, and it took real work. **Do not rewrite it in React.**

The job is to replace its hardcoded data constants with:
- a `fetch()` of current state on load, and
- an `EventSource` subscription for live updates.

Keep it framework-free for Phase 1. Defer any framework decision until the UI actually demands it.
A solo developer rewriting a working dashboard is weeks spent for zero new capability.

### Licence discipline

Every model, SDK and library that ships gets logged in `LICENCES.md` with its exact licence and
source. Specifically: **Ultralytics YOLO is AGPL-3.0 and must not enter this codebase or the vision
repo** — it is a commercial licensing trap for a product we sell. Apache-2.0 / MIT / BSD only.
Model licences vary per model and per release, not per vendor — check each one.

---

## 8. Repo layout

```
mine-monitor/
├── CLAUDE.md
├── LICENCES.md
├── docker-compose.yml
├── pyproject.toml
├── contracts/                  # JSON Schema for the v1 contracts. Source of truth.
├── alembic/
├── src/minemonitor/
│   ├── contracts/              # Pydantic models generated from / matching contracts/
│   ├── ingest/
│   │   ├── mqtt.py
│   │   ├── http.py
│   │   └── adapters/
│   │       ├── base.py         # DeviceAdapter interface
│   │       ├── teltonika.py    # Codec 8 / 8E over TCP
│   │       └── simulator.py    # replays synthetic movement — see §10
│   ├── zones/                  # polygon store, point-in-polygon, debounce/hysteresis
│   ├── rules/                  # zone rules, speed, dwell, offline detection
│   ├── cycles/                 # haul-cycle state machine and queue-time analytics
│   ├── events/                 # event creation, dedupe, ack workflow
│   ├── api/                    # FastAPI routers + SSE stream
│   ├── storage/                # SQLAlchemy models, repositories, retention jobs
│   └── config.py
├── web/
│   └── mine.html               # existing dashboard — wire to the API
└── tests/
```

---

## 9. Core logic worth specifying precisely

### Zone engine

- Zones are polygons in WGS84 with a `kind` (`loading` · `unloading` · `restricted` ·
  `speed_limited` · `generic`) and a rule payload.
- Point-in-polygon per incoming position, against zones for that site only.
- **Debounce is mandatory.** GNSS jitter on a boundary will otherwise generate hundreds of
  enter/exit events per hour and destroy trust in the system on day one. Require N consecutive
  fixes inside (default 2) and a minimum dwell before an entry is confirmed; require a hysteresis
  buffer (default 15 m) before an exit is confirmed. Make both configurable per zone.
- Emit `zone_entry` / `zone_exit` internally; emit `event.v1` only when a *rule* is breached.

### Rules

- `restricted` — entry by an unauthorised asset class → `critical`.
- `speed_limited` — sustained speed over limit **inside the zone it is actually in**, not a
  site-wide limit. Require N consecutive fixes over the threshold to avoid single-fix GPS spikes.
- `dwell` — asset stationary in a zone beyond a threshold.
- `asset_offline` — no position for N minutes while previously active. Distinguish
  *offline* from *stationary with ignition off* — they mean different things to a supervisor.
- Rules are data, not code. Adding a zone rule must not require a deploy.

### Haul-cycle analytics (the commercial payload)

A state machine per asset driven by zone transitions:

```
AT_FACE → HAULING_LOADED → AT_DUMP → RETURNING_EMPTY → AT_FACE
```

- A **cycle** is one complete traversal back to the same load zone.
- Segment durations come from transition timestamps.
- **Queue time** = time inside the load zone while stationary *before* loading begins (detected as
  stationary + in-zone + ignition on). This is the headline metric.
- Report per asset and per shift: cycle count, mean cycle time, and the segment breakdown
  including queue as a percentage.
- **Never hardcode a target or a benchmark figure.** We measure this mine's real number. The demo
  showed 17% — that was synthetic. Report what is actually observed.
- Cycles must be recomputable from stored positions, so a fix to the state machine can be applied
  retrospectively.

---

## 10. Build against a simulator, not hardware

Hardware will not be on site for weeks and cannot be assumed. **The very first adapter to build is
`adapters/simulator.py`** — it replays plausible machine movement (haul trucks looping pit face →
ROM pad → waste dump, an excavator working the face, a light vehicle wandering into a restricted
zone) and publishes `asset.position.v1` at a realistic 1 Hz.

This means the entire platform — zones, rules, cycles, alerts, dashboard — is fully developable and
demonstrable before a single tracker exists, and it becomes the fixture set for tests.

Everything the simulator emits must be indistinguishable, at the ingest boundary, from a real
tracker. If a bug only appears with real hardware, the abstraction was wrong.

---

## 11. Milestones and acceptance criteria

Build in this order. Each milestone must be demonstrable before starting the next.

**M1 — Skeleton and contracts**
`docker compose up` brings up Postgres/Timescale, Mosquitto, MinIO and the API. Contracts published
as JSON Schema with matching Pydantic models. Health endpoint green. Alembic migration creates
sites, assets, zones, positions (hypertable), events.
*Accept:* a position POSTed to the HTTP ingest endpoint is validated, stored, and readable back.

**M2 — Simulator and ingest**
Simulator produces a realistic shift of movement for ~9 assets. MQTT ingest path working.
Store-and-forward buffer survives a broker restart with no data loss.
*Accept:* kill the database mid-run, restart it, and no positions are lost or duplicated.

**M3 — Zones and rules**
Zone CRUD via API. Point-in-polygon with debounce and hysteresis. Restricted-entry, overspeed,
dwell and offline rules emitting `event.v1` into the alarm queue with ack workflow.
*Accept:* the simulated light vehicle entering the magazine raises exactly **one** critical event,
not a storm. Boundary-hugging assets raise **none**.

**M4 — Cycle analytics**
Haul-cycle state machine, segment breakdown, queue time. Per-shift rollups into
`asset.metrics.v1`.
*Accept:* against a simulated shift with known injected queue delays, computed queue time matches
the injected truth within 5%.

**M5 — Live dashboard**
`mine.html` wired to the real API: live asset positions on the site plan, real zones, real alarm
queue with acknowledgement, real cycle chart. SSE updates without a page refresh.
*Accept:* a supervisor can watch the simulated shift live and acknowledge an alarm, with no
hardcoded data left in the page.

**M6 — Hardening for site**
Retention jobs, audit log, backfill correctness for late data, per-site scoping enforced on every
query, basic auth and roles, one-command deploy.
*Accept:* restore onto a clean box from the compose file and a database dump, and it runs.

Only after M6 does real tracker hardware get integrated (`adapters/teltonika.py`), because by then
everything it feeds is already proven.

---

## 12. Engineering conventions

- Type hints everywhere; `ruff` clean; no bare `except`.
- All timestamps timezone-aware UTC. Never `datetime.now()` without a timezone.
- Coordinates: WGS84 lat/lon, in that order, as floats. Document any projected use explicitly.
- Units in field names where ambiguity is possible: `speed_kph`, `distance_m`, `dwell_s`.
- Every ingest boundary validates against the contract and rejects loudly. Never silently coerce
  malformed device data.
- Idempotent ingest: replaying the same position must not duplicate it or double-count a cycle.
- Structured JSON logging with `site_id` and `asset_id` on every operational log line.
- Tests: unit tests for zone geometry and the cycle state machine (these are where correctness
  bugs will actually live), integration tests over the simulator.
- No secrets in the repo. `.env.example` documents every variable.

---

## 13. Existing assets to bring into the repo

Copy these in from the current working directory before starting:

- **`minemonitor/mine.html`** → `web/mine.html`. The dashboard to wire up. Its inline SVG site plan,
  zone rendering and alarm table are the UI contract — read it early, it tells you what the API has
  to return.
- **`mining-addon/`** — a `coverage.py` rule and `configs/mine_crusher.yaml` for the vision repo.
  Not used in Phase 1; keep for reference on how vision events are shaped.
- **`mpm/`** — working prospectivity implementation. Separate concern, runs offline on assay data.
  Do not merge it into this service.

---

## 14. Open questions to surface, not guess

Flag these rather than inventing answers:

- Fleet list — makes, models, years. Decides whether any OEM telemetry is available.
- Whether any machine has onboard weighing (decides tonnes vs loads, permanently).
- Site connectivity — LTE coverage by area, any fibre or wireless backhaul.
- Whether the deployment must be fully on-premise.
- Site survey coordinates for real zone polygons; until then use plausible placeholders and keep
  them clearly marked as such.

---

## 15. The line that must not be crossed

Every output of this system is **advisory**. It warns a person. It does not brake a truck, stop a
conveyor, or actuate any plant. That boundary is the difference between a software product and a
functional-safety programme measured in years, and it is carried in the `advisory` field of every
event so it travels with the data.

If a requirement ever arrives that crosses it, stop and escalate rather than implementing it.
