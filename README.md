# Mine Monitor

Telemetry, geofencing and the alarm spine for mine operations — the Phase 1
platform behind eigenstate's first mine contract (a gold mine in Zimbabwe).

Phase 1 is deliberately **no AI, no cameras**: GNSS trackers, geofenced zones
with per-zone rules, a unified alarm queue, haul-cycle analytics (the commercial
payload — queue time at the face), and a live operations dashboard. See
[`CLAUDE.md`](./CLAUDE.md) for the full build brief.

## Status — M1 (skeleton & contracts)

- Data contracts published as JSON Schema in [`contracts/`](./contracts) with
  matching Pydantic v2 models.
- PostgreSQL 16 + TimescaleDB schema via Alembic (positions is a hypertable).
- FastAPI service: health endpoint, HTTP position ingest (validated, stamped,
  idempotent), and site-scoped read-back.
- `docker compose up` brings up the database, MQTT broker, MinIO and the API.

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

## Local development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

Tests run on SQLite for portability; the Postgres/TimescaleDB-specific behaviour
(hypertable, conflict handling) is exercised against the compose stack.

## Contracts

| Contract | Purpose |
|---|---|
| `asset.position.v1` | Raw GNSS telemetry (high volume). |
| `event.v1` | Anything worth a human's attention — one shape, one alarm queue. |
| `asset.metrics.v1` | Per-asset rollups (contract in M1, computed in M4). |

Every event is **advisory**: the platform warns people, it never actuates plant.

## Roadmap

M1 skeleton ✓ · M2 simulator + MQTT ingest · M3 zones & rules · M4 cycle
analytics · M5 live dashboard · M6 hardening for site.
