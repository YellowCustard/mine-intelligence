# Mine Monitor — Operations Manual

For the person running a deployed Mine Monitor instance: bringing it up, keeping it
healthy, and recovering it. Deployment mechanics live in
[`deploy/DEPLOY.md`](../deploy/DEPLOY.md); the release gate is in
[`deploy/RELEASE_CHECKLIST.md`](../deploy/RELEASE_CHECKLIST.md). This document is the
day-to-day and 2am-from-another-country runbook.

All times are stored in UTC and rendered in the site timezone
(`Africa/Harare`, UTC+2). Shifts cross midnight — a day boundary is not a shift
boundary.

---

## 1. What is running

One `docker compose` stack:

| Service | Role | Loss of it means |
|---|---|---|
| `db` | PostgreSQL 16 + TimescaleDB | Everything stops. The only stateful service that must be backed up. |
| `mqtt` | Mosquitto broker | New telemetry queues on the device spools; nothing is lost, backfilled on recovery. |
| `api` | FastAPI + dashboard + SSE | Dashboard and API down; ingest via MQTT continues into the ingestor. |
| `ingestor` | MQTT subscriber, rules, offline check, retention | No new events/metrics; positions still spool device-side. |
| `minio` | S3-compatible object store (evidence, later) | No effect in Phase 1. |
| `simulator` | Synthetic fleet (`--profile sim`) | Demo/test only; never in production. |

The API serves the dashboard on port **8000, bound to localhost**. HTTP Basic
credentials must ride TLS — never expose plain 8000 publicly. Reach it with an SSH
tunnel (`ssh -L 8000:127.0.0.1:8000 <user>@<host>`) or a TLS reverse proxy.

---

## 2. Bring it up

On the server, from the repo root:

```bash
bash deploy/deploy.sh          # core stack
bash deploy/deploy.sh --demo   # + simulated fleet (non-production only)
```

The script is idempotent — re-run it to apply an update. It refuses to continue
without a `.env` (it copies `.env.example` and stops so you can set real passwords).

**First admin.** If you did not set `MM_BOOTSTRAP_ADMIN_*` in `.env`, create one:

```bash
docker compose exec api uv run python -m minemonitor.auth.cli <username> admin
```

Roles are `viewer < supervisor < admin`, plus `device` (ingest only). Restrict a user
to one site with `--site <site_id>`; omit it for a global user.

---

## 3. Is it healthy?

Two endpoints, by design:

- `GET /healthz` — **liveness**: API + database only. Used by `deploy.sh` and any
  container orchestrator.
- `GET /health` — **full system**: also MQTT reachability and the ingestor heartbeat.
  Reports `degraded` if the ingestor heartbeat is older than `MM_HEARTBEAT_STALE_S`
  (default 180 s).

```bash
curl -fsS http://127.0.0.1:8000/healthz          # liveness
curl -fsS http://127.0.0.1:8000/health           # full system
curl -fsS http://127.0.0.1:8000/version          # build/version surface
docker compose ps                                # container states
docker compose logs -f --tail=100 ingestor       # follow the ingestor
```

The dashboard has a **System-health** view (ingest backlog, service heartbeats, disk,
backup status) and a **Data-quality** view (stale/missing/duplicate/impossible-move
/clock-skew indicators). A degraded state there distinguishes an **application**
failure from a **tracker** failure — they mean different things to a supervisor.

**Post-deploy smoke test** — run after any deploy or restore to confirm the whole
path (version, health, auth enforcement, login, ingest round-trip, dashboard):

```bash
MM_SMOKE_URL=http://127.0.0.1:8000 MM_SMOKE_USER=<admin> \
MM_SMOKE_PASSWORD=<pw> MM_SMOKE_SITE=kn-zw-01 \
docker compose exec -T api uv run python -m minemonitor.smoke
```

It exits 0 on success, 1 on any failed check, and prints a per-check report.

---

## 4. Reading the alarm queue

Every alarm is an `event.v1` in one unified queue, grouped by **severity**, not by
which sensor saw it. Every event is **advisory** — the system warns; it never controls
plant. Workflow:

- **Acknowledge** an alarm from the dashboard (or
  `POST /sites/{id}/events/{event_id}/ack`). State flows open → acknowledged →
  resolved.
- **Escalate to an incident** (`POST /sites/{id}/events/{event_id}/incident`) when an
  alarm needs investigation/assignment/resolution. Incidents link to the raw event by
  `event_id` and **never mutate it** — the telemetry record stays immutable.
- **Classify a delay** to explain lost time (loader unavailable, etc.). Classifications
  are annotations independent of telemetry, so analytics stay reproducible.
- **Shift handover** captures an auto-generated end-of-shift summary plus outgoing
  notes and the incoming crew's acknowledgement.

If an alarm *storm* appears (many enter/exit events on one asset), that is a
debounce/hysteresis symptom, not normal — see §8.

---

## 5. Backup and restore

The database is the only thing that must be backed up.

```bash
bash deploy/backup.sh                       # -> backups/minemonitor_<timestamp>.sql.gz
bash deploy/backup.sh /path/to/out.sql.gz   # explicit path
```

**Scheduled backups** — run the sidecar and set the cadence in `.env`:

```bash
docker compose --profile backup up -d
# MM_BACKUP_INTERVAL_S (default 86400) · MM_BACKUP_KEEP_DAYS (default 14)
```

**Restore onto a clean box** (the M6 acceptance path). Configure `.env` with the *same*
`POSTGRES_*` values as the source box, then:

```bash
bash deploy/restore.sh backups/minemonitor_20260905_120000.sql.gz
```

It starts only the database, waits for it, loads the dump (plain `.sql` or `.sql.gz`),
then brings up the rest of the stack. Verify with `docker compose ps` and
`curl -fsS http://127.0.0.1:8000/health`, then run the smoke test (§3).

---

## 6. Data retention and erasure (CDPA / SI 155 of 2024)

The mine is the data controller; eigenstate is the processor. Retention is
**configurable per data class**, in days, in `.env`; a scheduled deletion job runs on
`MM_RETENTION_INTERVAL_S` (≈ daily). `0` = keep forever.

| Class | Variable | Default |
|---|---|---|
| Raw positions | `MM_RETAIN_POSITIONS_DAYS` | 90 |
| Derived metrics + cycles | `MM_RETAIN_METRICS_DAYS` | 365 |
| Events | `MM_RETAIN_EVENTS_DAYS` | 365 |
| Audit trail | `MM_RETAIN_AUDIT_DAYS` | 730 |

Force a retention pass now: `POST /admin/retention/run` (admin).

**Personal-data requests.** Operator identity lives only in the `operators` table
(events reference it by foreign key, never by name). Export or erase per operator via
the operators endpoints; erasure is a tombstone that leaves history intact. There are
**no biometric templates** in this database — the gate face terminal yields only an
access event, never an image or template.

**Breach posture.** Reported obligation is 24 h to the regulator (POTRAZ) and 72 h to
affected individuals. The **audit log** records access to personal data and rule/zone
changes; use it to establish scope.

---

## 7. Devices and MQTT provisioning

Trackers authenticate to the broker with their `device_id` as the MQTT username, and
each is confined to writing only its own topic.

```bash
# Provision a device (admin, audited):
POST /sites/{site_id}/devices  { "device_id": "trk-1", "asset_id": "HT-102", "source": "teltonika" }
# Enable/disable:
POST /sites/{site_id}/devices/{device_id}/enabled  { "enabled": false }
```

One asset binds to one device (a second binding returns 409). Regenerate and reload the
broker ACL whenever devices change:

```bash
docker compose exec api uv run python -m minemonitor.devices.acl > mosquitto/acl
docker compose exec mqtt kill -HUP 1
```

**Strict mode.** With `MM_MQTT_REQUIRE_REGISTERED_DEVICE=true` the ingestor accepts
telemetry only for assets with an enabled device row. It is **off by default** so a
fresh or demo install ingests without provisioning; turn it on once devices are
provisioned. Independently of this flag, the ingestor always rejects any message whose
topic and payload disagree (anti-spoof).

---

## 8. Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `/health` degraded, `/healthz` OK | Ingestor heartbeat stale or MQTT unreachable. `docker compose logs ingestor mqtt`; check `MM_HEARTBEAT_STALE_S`. |
| API won't start under `MM_ENV=prod` | The production guard rejected sample DB credentials or a weak bootstrap password. Set real secrets in `.env`. The log line names the offending value. |
| Alarm storm on a boundary-hugging asset | Debounce/hysteresis too tight for local GNSS quality. Widen the per-zone enter-fix count / exit buffer (defaults: 2 fixes in, 15 m out). |
| Positions stop arriving | Broker or link down — devices spool locally and backfill on recovery; nothing is lost. Confirm with the device spool and `received_at` vs `ts` gaps. |
| Late/out-of-order data changed a cycle | Expected and handled — cycles are recomputable. Re-run cycle recompute; the state machine is deterministic from stored positions. |
| Duplicate-looking positions | Ingest is idempotent; a replay does not double-store or double-count. If counts still look off, check `received_at`. |
| "No space left on device" | Free space (old dumps in `backups/`, container logs); deletes succeed while writes fail. Then prune with `MM_BACKUP_KEEP_DAYS`. |
| Dashboard shows nothing live | SSE connection — confirm `GET /sites/{id}/stream` reachable through the proxy/tunnel and auth is valid. |
| Account locked out | `MM_LOGIN_MAX_FAILURES`/`MM_LOGIN_LOCKOUT_MINUTES`. An admin re-setting the user's credentials via the CLI clears the lock. |

---

## 9. Routine operations checklist

- **Daily:** confirm `/health` green; glance at System-health and Data-quality;
  confirm a backup was written (`ls -lh backups/`).
- **Per shift:** review the alarm queue, resolve/acknowledge, complete handover.
- **Weekly:** confirm scheduled backup pruning is sane; skim the audit log.
- **After any deploy/restore:** run the smoke test (§3).
- **When devices change:** regenerate and reload the broker ACL (§7).

---

*Every output of this system is advisory. It warns a person. It does not brake a
truck, stop a conveyor, or actuate any plant.*
