# Mine Monitor

Telemetry, geofencing and the alarm spine for mine operations — the Phase 1
platform behind eigenstate's first mine contract (a gold mine in Zimbabwe).

Phase 1 is deliberately **no AI, no cameras**: GNSS trackers, geofenced zones
with per-zone rules, a unified alarm queue, haul-cycle analytics (the commercial
payload — queue time at the face), and a live operations dashboard. See
[`CLAUDE.md`](./CLAUDE.md) for the full build brief.

## Status — M1 skeleton · M2 MQTT · M3 zones & rules · M4 cycle analytics · M5 live dashboard

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
- `docker compose up` brings up the database, MQTT broker, MinIO, the API and the
  ingestor; `docker compose --profile sim up` adds the simulator. The dashboard
  is at `http://localhost:8000/`.

## Quickstart

```bash
cp .env.example .env
docker compose up --build
```

Then:

```bash
# Health
curl localhost:8000/health

# Ingest a position (received_at is stamped server-side)
curl -X POST localhost:8000/ingest/positions -H 'content-type: application/json' -d '{
  "schema": "asset.position.v1", "site_id": "kn-zw-01", "asset_id": "HT-102",
  "ts": "2026-09-05T11:42:07Z", "lat": -17.8252, "lon": 31.0335,
  "speed_kph": 47.0, "source": "curl"
}'

# Read it back
curl "localhost:8000/sites/kn-zw-01/positions?asset_id=HT-102"
```

API docs are served at `localhost:8000/docs`.

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
migration that creates the `positions` hypertable.

## Contracts

| Contract | Purpose |
|---|---|
| `asset.position.v1` | Raw GNSS telemetry (high volume). |
| `event.v1` | Anything worth a human's attention — one shape, one alarm queue. |
| `asset.metrics.v1` | Per-asset rollups (contract in M1, computed in M4). |

Every event is **advisory**: the platform warns people, it never actuates plant.

## Roadmap

M1 skeleton ✓ · M2 simulator + MQTT ingest ✓ · M3 zones & rules ✓ · M4 cycle
analytics ✓ · M5 live dashboard ✓ · M6 hardening for site.
