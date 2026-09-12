# Mine Monitor — Architecture

Developer-facing architecture reference. Read [`CLAUDE.md`](../CLAUDE.md) first for
the *why*; this document is the *how* and *where*. It describes the system as it is
actually built, not as it was planned.

---

## 1. One-paragraph summary

Mine Monitor is a single Python service (FastAPI) over one database
(PostgreSQL 16 + TimescaleDB), plus a Mosquitto broker and MinIO, all brought up by
one `docker compose`. GNSS trackers (or the simulator) publish `asset.position.v1`
over MQTT or HTTP; the ingestor normalises and durably stores every fix; a zone
engine with debounce/hysteresis and a data-driven rules engine turn transitions into
`event.v1` records in a unified alarm queue; a cycle state machine derives haul-cycle
and queue-time analytics as `asset.metrics.v1`; and a framework-free HTML dashboard
reads current state over REST and live updates over Server-Sent Events. Everything is
multi-tenant by `site_id`, advisory-only, audited, and self-hostable on one box.

---

## 2. The five layers

Mirrors the pattern from the vision project so the two systems share contracts and
operational habits.

```
L0  Devices        GNSS trackers · simulator · (later: cameras, gate face terminal)
L1  Edge/ingest    Protocol adapters → normalise → store-and-forward spool
L2  Platform core  Ingest gateway · zone engine · rules engine · cycle analytics ·
                   events · storage · API · SSE
L3  Applications   Operations dashboard · alarm queue · reports · exports
L4  Learning loop  (Phase 2+, in the vision repo — not here)
```

**The rule that keeps it extensible:** every source speaks the same event contract.
A geofence breach, a camera detection and a gate access all arrive as `event.v1` and
land in the same alarm queue. The control room groups by *severity*, never by source.
Anything that cannot be expressed as `event.v1` or `asset.metrics.v1` is a design
smell — fix the contract, do not special-case it.

---

## 3. Package map (`src/minemonitor/`)

| Package | Responsibility |
|---|---|
| `contracts/` | Pydantic v2 models matching the JSON Schema in `/contracts`. Validation happens here, at the ingest boundary. |
| `ingest/` | `http.py` (POST path), `mqtt.py` (broker subscriber + spool drain), `authz.py` (topic↔payload anti-spoof), `adapters/` (`base.py` interface, `simulator.py`, `teltonika.py` stub). |
| `zones/` | Polygon store, point-in-polygon, debounce/hysteresis state. |
| `rules/` | Restricted-entry, overspeed, dwell, offline. Rules are **data on the zone**, not code. |
| `cycles/` | `statemachine.py` (pure, reproducible), `recompute.py`, `shifts.py` (cross-midnight shift bounds). |
| `events/` | Event creation, dedupe, ack workflow, incident linkage. |
| `devices/` | `service.py` (provisioning), `acl.py` (Mosquitto ACL renderer). |
| `api/` | FastAPI app (`main.py`), routers under `api/routers/`, SSE stream. |
| `storage/` | SQLAlchemy 2.x models, `db.py` engine/session factory, retention + backup helpers, heartbeat. |
| `auth/` | HTTP Basic, PBKDF2 hashing, role deps, lockout, `cli.py`. |
| `config.py` | Pydantic-settings; all `MM_*` env vars; the production fail-fast guard. |
| `logging_config.py` | Structured JSON logging + request-id correlation filter. |
| `smoke.py` | Post-deploy smoke test (importable + `python -m`). |

---

## 4. Data contracts (the spine)

Published as JSON Schema in [`/contracts`](../contracts) and mirrored as Pydantic
models. Version them; validate at every ingest boundary; reject malformed device data
loudly — never silently coerce.

- **`asset.position.v1`** — raw telemetry, high volume. Carries both `ts` (device
  time) and `received_at` (server time); the gap between them is how we detect and
  correct buffered backfill. Stored immutably.
- **`event.v1`** — anything worth a human's attention. `advisory` is **always
  `true`** — the platform warns people, it never actuates plant (see §9). Types:
  `zone_breach`, `overspeed`, `zone_dwell`, `asset_offline`, `geofence_exit` (later,
  from other sources: `proximity`, `access_granted`, …). `source` names the sensing
  modality; the dashboard groups by severity, not source.
- **`asset.metrics.v1`** — derived rollups per asset per 5-minute bucket. Never
  hand-edited; always recomputable from `positions`.

Keeping these compatible with the vision repo's `event.v1` means a camera node can
publish into this platform with no translation layer.

---

## 5. The immutability rule (why annotations live in separate tables)

`positions` is **immutable raw telemetry**. Derived analytics (`haul_cycles`,
`asset_metrics`) are **reproducible** — recomputable from `positions` at any time, so
a fix to the cycle state machine can be re-applied retrospectively. Human/operational
annotations — incidents, delay classifications, notes, handovers — live in **separate
tables** and never mutate the raw `Event` or the telemetry they describe (they link by
`event_id`). This is what lets the derived layer stay trustworthy: nothing a person
does in the UI can corrupt the source of truth or make analytics unreproducible.

Everywhere a value is shown, the system distinguishes **observed** facts from
**inferred/estimated** values (e.g. equipment state carries a `basis` of
`observed | inferred` and a `data_age_s`). Never present an estimate as a measurement.

---

## 6. Storage schema and migrations

One database. TimescaleDB **hypertables** for the high-volume time series
(`positions`, `asset_metrics`); ordinary tables for everything else. Migrations are
Alembic, single linear head, additive and safe for an existing install:

| Migration | Adds |
|---|---|
| `0001_initial` | sites, assets, positions (hypertable) |
| `0002_zones_state` | zones, zone transition state |
| `0003_cycles_metrics` | haul_cycles, asset_metrics (hypertable) |
| `0004_auth_audit` | users, audit_log |
| `0005_operators` | operators (the single PII home) |
| `0006_lockout_heartbeat` | login lockout, service heartbeat |
| `0007_events_index` | event query index |
| `0008_shift_definitions` | per-site configurable shift definitions |
| `0009_incidents_delays` | incidents, delay_classifications |
| `0010_shift_handovers` | shift_handovers |
| `0011_incident_version` | incident optimistic-locking version column |
| `0012_devices` | devices (MQTT provisioning + broker ACL source) |

Every operational table carries `site_id` and every query is scoped by it. New
annotation tables never touch `positions`.

---

## 7. Ingest, store-and-forward, idempotency

The network and power **will** fail; the whole ingest side is designed around it.

- **Publisher spool** (`MM_SPOOL_PATH`, a local SQLite file): the device/simulator
  side buffers fixes crash-safely and backfills on reconnect. Accepts out-of-order
  and late-arriving data without corrupting derived analytics.
- **Idempotent ingest:** replaying the same position must not duplicate it or
  double-count a cycle. The ingestor withholds MQTT acknowledgement until a fix is
  durably stored, so a **database restart loses nothing and duplicates nothing** (M2
  acceptance).
- **MQTT broker auth** (`ingest/authz.py` + `devices/`): the broker is the transport
  authority. Internal publishers (ingestor, simulator, Teltonika adapter) authenticate
  as one **service account** (`MM_MQTT_USERNAME`/`PASSWORD`); each native-MQTT tracker
  authenticates with its `device_id`. Provisioning issues a per-device secret (returned
  once; only its Mosquitto `$7$` hash is stored). `python -m minemonitor.devices.broker_config`
  renders the broker **password file** and **ACL** from the `devices` table — the ACL
  confines each device to writing only its own `mm/<site>/<asset>/position` topic. The
  hardened broker config (`docker/mosquitto.auth.conf`) runs `allow_anonymous false`;
  the dev/demo default stays anonymous, and `MM_ENV=prod` refuses to boot on an
  anonymous broker. Above the broker, the ingestor still independently rejects any
  message whose topic and payload disagree (anti-spoof), and, when
  `MM_MQTT_REQUIRE_REGISTERED_DEVICE=true`, rejects assets with no enabled device row.

---

## 8. Zone engine, rules, cycles

- **Zone engine:** WGS84 polygons per site, point-in-polygon per incoming fix against
  that site's zones only. **Debounce is mandatory** — N consecutive fixes inside
  (default 2) plus a minimum dwell to confirm entry, and a hysteresis buffer
  (default 15 m) to confirm exit. Both configurable per zone. Emits `zone_entry` /
  `zone_exit` internally; emits `event.v1` only when a *rule* is breached. This is
  what stops GNSS jitter producing an alarm storm (M3 acceptance: the light vehicle in
  the magazine raises exactly **one** critical event; boundary-huggers raise none).
- **Rules are data, not code** — adding a zone rule must not require a deploy.
  `restricted` (unauthorised class → critical), `speed_limited` (sustained speed over
  the limit *of the zone it is in*, N consecutive fixes), `dwell` (stationary beyond a
  threshold), `asset_offline` (no fix for N minutes while previously active —
  distinguished from *parked with ignition off*).
- **Haul-cycle analytics** (the commercial payload): a per-asset state machine driven
  by zone transitions, `AT_FACE → HAULING_LOADED → AT_DUMP → RETURNING_EMPTY →
  AT_FACE`. **Queue time** = stationary + in-load-zone + ignition-on, *before* loading
  begins — the headline metric. No target or benchmark is ever hardcoded; we report
  this mine's observed number. The state machine is a pure function, so cycles are
  recomputable from stored positions (idempotent `recompute`).

---

## 9. The advisory line

Every output is **advisory** — carried in the `advisory: true` field of every event so
the distinction travels with the data, not a slide. The platform warns a person. It
never brakes a truck, stops a conveyor, or actuates any plant. Crossing that line is a
different product with a functional-safety burden measured in years. If a requirement
arrives that crosses it, stop and escalate rather than implement it.

---

## 10. API and live updates

FastAPI with per-route role dependencies. Reads are `GET`; mutations are audited. Live
UI updates use **Server-Sent Events** (`GET /sites/{id}/stream`) — one-way
server→browser, simpler than WebSockets and more tolerant of a flaky link. The
dashboard (`web/mine.html`) is framework-free: a `fetch()` of current state on load
plus an `EventSource` subscription. It is **extended, not rewritten** — the inline-SVG
site plan and alarm table are the UI contract.

**API versioning.** Every domain router is served both at its historical unprefixed
path (the dashboard and existing clients) and under **`/api/v1`** (new clients —
mobile, integrations). The unprefixed paths are the compatibility surface and are
unchanged. A Phase-1 **platform** surface is versioned-only:
`GET /api/v1/platform/contracts` (the versioned event contracts the platform
understands) and `GET /api/v1/platform/metrics` (admin; process-local observability
counters). The extension substrate behind it — a contract registry, an in-process
event bus, and a metrics registry — lives in `minemonitor/platform/`; see
`docs/PLATFORM_EVOLUTION_ARCHITECTURE.md`.

Route surface (roles: `viewer < supervisor < admin`, plus `device` for ingest only):

- **Ops/health:** `/healthz` (liveness), `/health` (full-system, incl. MQTT +
  ingestor heartbeat), `/version`, `/me`, `/me/capabilities`.
- **Ingest & telemetry:** `POST /ingest/positions`,
  `GET /sites/{id}/positions`, `GET /sites/{id}/state`, `GET /sites/{id}/stream` (SSE).
- **Zones:** CRUD under `/sites/{id}/zones`.
- **Alarm queue:** `POST /sites/{id}/events/{event_id}/ack`,
  `POST /sites/{id}/events/{event_id}/incident`.
- **Notifications:** `GET /sites/{id}/notifications` (supervisor+) — the
  store-and-forward alert outbox. Events above `MM_NOTIFY_MIN_SEVERITY` enqueue a
  webhook/email notification in the same transaction; the ingestor drains it with
  backoff. Advisory, `event.v1`-only payload (no operator names).
- **Analytics:** cycles, metrics, recompute; operations scorecard, exceptions,
  trends, bottlenecks, data-quality, system-health; shift-definitions CRUD,
  `shifts/current`, shift-summary; `reports/shift`, `reports/daily`.
- **Incidents / delays / handovers:** lifecycle transitions + notes; delay
  classification (+ categories); handover create/acknowledge.
- **Devices:** `GET/POST /sites/{id}/devices`,
  `POST /sites/{id}/devices/{device_id}/enabled` (admin, audited).
- **Fuel** (Phase 3): `POST/GET /sites/{id}/fuel/tanks` (admin/viewer),
  `POST/GET /sites/{id}/fuel/transactions` (supervisor/viewer, audited; publishes
  `fuel.transaction.v1`), `POST /sites/{id}/fuel/tank-readings`,
  `GET /sites/{id}/fuel/consumption` (measured litres + calculated efficiency,
  labelled), `GET /sites/{id}/fuel/reconciliation/{tank_id}` (flags variance, never
  corrects). Measured facts only; anomaly detection is a later increment.
- **Governance:** operators (+ export), audit log, `POST /admin/retention/run`.

---

## 11. Security, observability, compliance

- **Auth:** HTTP Basic (must ride TLS — the API binds localhost by default). PBKDF2-
  HMAC-SHA256, constant-time verify, DB-backed lockout, a short verify cache. First
  admin is bootstrapped from `MM_BOOTSTRAP_ADMIN_*` on an empty users table, or created
  with `python -m minemonitor.auth.cli`.
- **Production guard:** with `MM_ENV=prod`, `config.py` refuses to boot on sample DB
  credentials or a weak bootstrap password — a misconfigured box fails loudly.
- **Observability:** structured JSON logs with `site_id`/`asset_id`, a request-
  correlation `X-Request-ID` (contextvar + logging filter, sanitised against log
  injection), a safe error boundary that returns a generic 500 with the request id and
  no stack leak, and security-headers middleware (CSP, nosniff, frame-options,
  referrer-policy). Concurrency-sensitive writes (incidents) use optimistic locking.
- **Compliance (CDPA + SI 155 of 2024):** the mine is the controller, eigenstate the
  processor — everything scoped, exportable and deletable per site. **No biometric
  templates, ever** — the gate terminal yields only an access event. Operator identity
  is a foreign key, never a name in an event payload; the `operators` table is the
  single PII home, supporting export and tombstone erasure. Retention is configurable
  per data class with a scheduled deletion job that actually runs. Audit log on access
  to personal data and on rule/zone changes.

---

## 12. Testing and the local gate

- Unit tests for zone geometry and the cycle state machine (where correctness bugs
  actually live); integration tests over the simulator, which is indistinguishable
  from a real tracker at the ingest boundary.
- The local gate before every push: `ruff check` · `ruff format --check` ·
  `mypy src` · `pytest`. Full-fidelity runs use real Postgres 16 + Mosquitto and an
  `alembic upgrade head` (with a down/up round-trip for new migrations).

---

## 13. Where to change things

| To… | Edit |
|---|---|
| Add/adjust a zone rule | Zone data via the API — no deploy. |
| Add an event type | `contracts/event.v1` + the emitting rule/adapter; keep vision-compatible. |
| Add a device protocol | A new adapter under `ingest/adapters/` implementing `base.py`. |
| Change retention | `MM_RETAIN_*_DAYS` env; the job runs on `MM_RETENTION_INTERVAL_S`. |
| Add a schema table | A new additive Alembic migration off the current head; carry `site_id`. |
| Tune debounce/hysteresis | Per-zone config (defaults: 2 fixes in, 15 m out). |

---

*This system is advisory. It warns people; it does not control machines.*
