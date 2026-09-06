# Mine Monitor

Telemetry, geofencing and the alarm spine for mine operations — the Phase 1
platform behind eigenstate's first mine contract (a gold mine in Zimbabwe).

Phase 1 is deliberately **no AI, no cameras**: GNSS trackers, geofenced zones
with per-zone rules, a unified alarm queue, haul-cycle analytics (the commercial
payload — queue time at the face), and a live operations dashboard. See
[`CLAUDE.md`](./CLAUDE.md) for the full build brief.

## Status — M1 skeleton · M2 MQTT · M3 zones & rules · M4 cycle analytics · M5 live dashboard · M6 hardening

- Data contracts published as JSON Schema in [`contracts/`](./contracts) with
  matching Pydantic v2 models.
- PostgreSQL 16 + TimescaleDB schema via Alembic (positions is a hypertable).
- FastAPI service: health endpoint, HTTP position ingest (validated, stamped,
  idempotent), and site-scoped read-back.
- **Simulator** replaying a realistic shift for ~9 machines (haul trucks with a
  shared loader so real queue time emerges, an excavator, patrol vehicles, and a
  light vehicle that wanders into the restricted magazine).
- **MQTT ingest** with store-and-forward: a crash-safe publisher spool that
  survives a broker restart, and an ingestor that withholds acknowledgement until
  a position is durably stored — so a **database restart loses nothing and
  duplicates nothing** (ingest is idempotent).
- **Zones & rules** — GeoJSON polygons per site with point-in-polygon plus
  **debounce and hysteresis** (N consecutive fixes to enter, a metre buffer to
  exit) so GNSS jitter never produces an alarm storm. Rules are data on the zone
  (`restricted`, `speed_limited`, `dwell`) plus a periodic `asset_offline` check
  that distinguishes offline from parked. Breaches land in the unified alarm queue
  as `event.v1`, with an acknowledgement workflow. Zone CRUD via the API.
- **Haul-cycle analytics** — a state machine driven by zone transitions
  (`AT_FACE → HAULING_LOADED → AT_DUMP → RETURNING_EMPTY`) computes per-cycle
  segment durations and the headline **queue time at the face**, recomputable
  from stored positions. Per-asset `asset.metrics.v1` 5-minute buckets (distance,
  moving/idle, speeds, zone dwell, loads) and a per-shift summary (cycle count,
  mean cycle time, segment breakdown incl. queue %). No target is ever hardcoded —
  we report this mine's observed numbers. API: cycles, shift-summary, metrics, and
  an idempotent recompute.
- **Live operations dashboard** — the existing `web/mine.html` wired to the real
  API (not rewritten): the site plan, geofences, asset markers, fleet and alarm
  tables and cycle chart are driven by a `fetch()` of current state plus a
  **Server-Sent Events** stream for live updates, with in-page **alarm
  acknowledgement**. Lat/lon is projected onto the SVG site plan; no hardcoded
  operational data remains, and Phase-1 non-goals (tonnes, fuel) are not shown.
  Served by the API at `/`.
- **Hardening for site** — **HTTP Basic auth** with a role hierarchy
  (`viewer` < `supervisor` < `admin`, plus a `device` role for ingest), enforced
  on every route and **scoped per site** so a site-scoped user cannot read or
  touch another site. An **append-only audit log** records rule/zone changes,
  acknowledgements and retention runs. **Per-data-class retention** (positions,
  metrics, events — each configurable in days, `0` = keep forever) runs as a
  scheduled deletion job in the ingestor and is itself audited. Backfilled and
  out-of-order positions are corrected on recompute (analytics are
  order-independent). Password hashing is PBKDF2-HMAC-SHA256 from the standard
  library — no new dependency.
- `docker compose up` brings up the database, MQTT broker, MinIO, the API and the
  ingestor; `docker compose --profile sim up` adds the simulator. The dashboard
  is at `http://localhost:8000/` and now prompts for credentials.

## Quickstart

```bash
cp .env.example .env
# Set a first-run admin so the dashboard is reachable (or create one later
# with the CLI — see below):
#   MM_BOOTSTRAP_ADMIN_USER=admin MM_BOOTSTRAP_ADMIN_PASSWORD=change-me
docker compose up --build
```

Then (every route except `/health` needs HTTP Basic credentials):

```bash
# Health (public)
curl localhost:8000/health

# Who am I
curl -u admin:change-me localhost:8000/me

# Ingest a position — requires a device account (create one, see below).
# received_at is stamped server-side.
curl -u dev1:devpass -X POST localhost:8000/ingest/positions \
  -H 'content-type: application/json' -d '{
  "schema": "asset.position.v1", "site_id": "kn-zw-01", "asset_id": "HT-102",
  "ts": "2026-09-05T11:42:07Z", "lat": -17.8252, "lon": 31.0335,
  "speed_kph": 47.0, "source": "curl"
}'

# Read it back (viewer or higher)
curl -u admin:change-me "localhost:8000/sites/kn-zw-01/positions?asset_id=HT-102"
```

### Users and roles (M6)

Roles form a hierarchy — `viewer` < `supervisor` < `admin` — plus a separate
`device` role that may only ingest. Site-scoped users see a single site; leave
the site blank for a global user. Create users with the CLI (it reads the
password from `MM_NEW_USER_PASSWORD` or prompts):

```bash
docker compose exec api uv run python -m minemonitor.auth.cli alice admin
docker compose exec api uv run python -m minemonitor.auth.cli dev1 device
docker compose exec api uv run python -m minemonitor.auth.cli bob viewer --site kn-zw-01
```

An admin can also create users over the API (`POST /users`) and read the audit
trail (`GET /sites/{site_id}/audit`).

### Operators & data-subject rights (personal data, brief §4)

Operator identity is a **foreign key, never a name in a payload**. All personal
data lives in the single `operators` table; events and haul cycles reference an
operator only by an opaque `operator_id`. This is the surface a Cyber & Data
Protection Act / SI 155 request runs through, and every read of a personal record
is audited:

```bash
# admin only, path-scoped to the site
curl -u admin:… -X POST localhost:8000/sites/kn-zw-01/operators \
  -H 'content-type: application/json' -d '{"display_name":"…","employee_ref":"…"}'
curl -u admin:… localhost:8000/sites/kn-zw-01/operators/<id>/export   # data-subject access
curl -u admin:… -X DELETE localhost:8000/sites/kn-zw-01/operators/<id>  # erasure
```

Erasure is a **tombstone**: the personal columns are nulled and `erased_at`
stamped, while the opaque id and every historical foreign key are preserved — a
deletion of the person never rewrites the operational record. Retention now
covers the audit trail too (`MM_RETAIN_AUDIT_DAYS`, kept longer than the data it
describes). No biometric or face data is ever stored — that stays in the vendor
gate appliance.

API docs are served at `localhost:8000/docs`.

### Health & resilience (Phase 2)

- `GET /healthz` — **liveness**: 200 while the API and its database are up (the
  api container's healthcheck).
- `GET /health` — **full-system**: also reports MQTT reachability and the
  ingestor's heartbeat, and returns 503 when any is down, so a stuck ingestor or
  dead broker is visible. The ingestor (no HTTP surface) is healthchecked with
  `python -m minemonitor.healthcheck`, which passes while its heartbeat is fresh.
- Every long-running compose service has `restart: unless-stopped` and a
  healthcheck (power and network *will* fail at a remote site).
- **Auth hardening:** an account locks after `MM_LOGIN_MAX_FAILURES` consecutive
  failures for `MM_LOGIN_LOCKOUT_MINUTES` (crash-safe in the DB); failures and
  lockouts are audited. A short verify cache avoids re-deriving PBKDF2 on every
  request (the SSE stream reconnects and polls continuously) without weakening
  lockout — a locked account is refused even on a cache hit.
- **Scheduled backups:** `docker compose --profile backup up -d` dumps the
  database to `./backups` on an interval and prunes old dumps.

### Watch the simulated fleet

```bash
docker compose --profile sim up          # simulator → MQTT → ingestor → DB
curl "localhost:8000/sites/kn-zw-01/positions?limit=20"
```

The simulator publishes `asset.position.v1` to `mm/{site_id}/{asset_id}/position`
at 1 Hz. The ingestor subscribes, validates, stamps `received_at`, and stores
idempotently — identical at the ingest boundary to a real tracker.

## Local development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

Tests run on SQLite for portability. The Postgres-specific behaviour (idempotent
`ON CONFLICT` insert, JSONB, tz-aware round-trip) is covered by integration tests
that run when a database URL is provided:

```bash
MM_TEST_DATABASE_URL=postgresql+psycopg://minemonitor:minemonitor@localhost:5432/minemonitor \
  uv run pytest
```

CI runs the full suite against a real TimescaleDB service, including the
migration that creates the `positions` hypertable. It also runs `ruff`, a `mypy`
type check, and a `pip-audit` dependency scan:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pip-audit
```

## Contracts

| Contract | Purpose |
|---|---|
| `asset.position.v1` | Raw GNSS telemetry (high volume). |
| `event.v1` | Anything worth a human's attention — one shape, one alarm queue. |
| `asset.metrics.v1` | Per-asset rollups (contract in M1, computed in M4). |

Every event is **advisory**: the platform warns people, it never actuates plant.

## Roadmap

M1 skeleton ✓ · M2 simulator + MQTT ingest ✓ · M3 zones & rules ✓ · M4 cycle
analytics ✓ · M5 live dashboard ✓ · M6 hardening for site (auth & roles,
per-site scoping, audit log, per-class retention, backfill correctness,
one-command deploy) ✓.

Phase 1 (M1–M6) is complete. Post-Phase-1 work is tracked as a four-phase
roadmap: **compliance / data-protection** (operator personal-data model, export &
erasure, audit-log retention — landed); **ops readiness & auth hardening**
(restart policies, health probes, ingestor heartbeat, login lockout, scheduled
backups — landed); then quality & CI (mypy and pip-audit already gate CI; more
test coverage and a Playwright dashboard smoke test to come), and finally real
tracker hardware (`adapters/teltonika.py`) — everything it feeds is already
proven against the simulator.
