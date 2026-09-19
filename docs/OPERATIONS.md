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

**Zone-occupancy alarms.** A zone can carry a `max_occupancy` in its rule payload; when
more assets are confirmed inside it than the cap (the "6 people in a 5-person sector"
signal), the ingestor raises one `zone_occupancy` `event.v1` into the same queue and
holds it open (deduped) until acknowledged. It is a cross-asset aggregate checked on the
maintenance tick, source-agnostic (GNSS today, vision tracks later), and advisory.

**Cameras.** The camera registry (`/api/v1/sites/{id}/cameras`, admin-managed, audited)
inventories the site's fixed cameras and their AI-readiness metadata. It is the intake
tool for the RAN Mines estate; no video passes through the platform — only structured
events. Vision perception itself is a later edge subsystem (see `docs/VISION_*`).

**NVR AI-event ingestion (Vision Stage A).** The near-term, no-new-hardware vision path
(`docs/VISION_BUILD_GATE.md` §4.5/§8): the Dahua NVR's existing AI "smart" events
(line-crossing, intrusion, loitering) are normalised to `vision.vendor_event.v1` and promoted
into the **same** unified alarm queue as GNSS/gate events (`source: "nvr:<cam>"`). This is
**vendor event ingestion, not first-party perception** — every record and alarm is labelled
`provenance: vendor_inferred`, the NVR's own `vendor_confidence` is carried verbatim and never
shown as a first-party confidence, and **identity-bearing NVR types (face) are refused at the
boundary** — no biometric field ever lands. A camera channel resolves to a registered camera
(a `dahua_nvr:<channel>` placeholder when unmapped). Ingest is idempotent on a deterministic
id, so replay/backfill never double-alarms. The path is developed simulator-first
(`ingest/adapters/dahua_nvr_sim.py`); the live poller and the exact vendor field mapping
(`nvr.py`) are confirmed against the real Dahua backdoor-API on the site visit before go-live.

**Access control.** Gate/turnstile access decisions are ingested as `access.event.v1`
(`/api/v1/sites/{id}/access/events`) — a record only; the gate hardware enforces. **No
biometric template or image ever enters the platform**: `credential_ref` is opaque and
identity is a foreign key to an operator. Mine Monitor then applies its own authorisation
rules — an operator who is **suspended**, **not inducted**, or **off-shift** should be
rejected — and when the gate *granted* entry to such a person, a critical `access_denied`
`event.v1` is raised (an unauthorised entry the gate let through). An admin sets an
operator's access status via `POST /sites/{id}/operators/{operator_id}/access-status`.

*Random search & metal detector.* With `MM_ACCESS_SEARCH_RATE_PERCENT` set, Mine Monitor
deterministically selects that share of passages for a physical search. A guard records the
outcome via `POST /sites/{id}/access/events/{event_id}/search` (with the metal-detector
result); a positive detection raises a critical `metal_detected` alarm. A selected passage
left unsearched past `MM_ACCESS_SEARCH_GRACE_S` raises a `search_missed` alarm on the
maintenance tick — the audit trail that a required search was skipped.

*Live gate feed.* Set `MM_ACCESS_GATE_URL` (+ `MM_ACCESS_GATE_TOKEN`) to have the ingestor
poll the Dahua gate API each tick and ingest new access events automatically; blank = off
(the HTTP-ingest endpoint and simulator still work). It resumes cleanly after a restart or a
link outage (the cursor is the last stored event; ingest is idempotent). **Verify the vendor
response mapping against the real backdoor-API spec before relying on it in production.**

**Laboratory data (FP-08).** Assay results are ingested directly from the lab instrument
(the client's Agilent 2000-series AA spectrometer via SpectrAA) so a result is never retyped:
either one at a time (`POST /api/v1/sites/{id}/laboratory/results`) or a whole SpectrAA CSV
export (`POST .../laboratory/results/csv`). Each result is stored **write-once** as a
`laboratory.result.v1` with the raw original preserved and **hashed** (SHA-256) — so tampering
with, or a divergent re-export of, a stored result is detectable and raises a
`lab_result_conflict` flag rather than overwriting the original. Corrections **never mutate**
a result: `POST .../laboratory/results/{result_id}/corrections` appends a
`laboratory.correction.v1` carrying the corrected value, the **actor** and a reason (audited);
the current value is the latest correction, exposed alongside the immutable measured value and
full history on `GET .../laboratory/results/{result_id}`. Set `MM_LAB_ANOMALY_BOUNDS` (JSON,
element → `[min, max]` in the result's unit) to have a value outside its element's range raise
an advisory `lab_anomaly` alarm for review — deterministic bounds first, and it **flags, never
alters** a measured record. Blank = anomaly detection off. `sample_ref` is an opaque lab label,
never personal data. **The real SpectrAA export layout is confirmed on site; the CSV column
mapping lives in one function (`ingest/adapters/spectraa.py::_row_to_raw`) to adjust then.**

**Notifications (alert egress).** Because nobody watches the dashboard around the
clock at a remote site, qualifying events are pushed out. It is **off by default**;
turn it on in `.env`:

- `MM_NOTIFY_MIN_SEVERITY` — `info` | `warning` | `critical` (blank = off).
- A **webhook** (`MM_NOTIFY_WEBHOOK_URL`, POSTs the event JSON), **email**
  (`MM_NOTIFY_SMTP_*`, `MM_NOTIFY_EMAIL_TO`), and/or **WhatsApp**
  (`MM_NOTIFY_WHATSAPP_URL` + `MM_NOTIFY_WHATSAPP_TOKEN` + `MM_NOTIFY_WHATSAPP_TO`, the
  WhatsApp Cloud API). WhatsApp is off unless both URL and recipients are set; its
  message is a single advisory line (`[severity] site: summary (advisory)`), never an
  operator name. All channels are self-hostable except WhatsApp, which uses the operator's
  own Cloud API credentials.

Delivery is **store-and-forward**: a notification is written in the same transaction
as its event, then a background dispatcher sends it with exponential backoff
(`MM_NOTIFY_RETRY_BASE_S`), so an outage delays but never loses an alert. After
`MM_NOTIFY_MAX_ATTEMPTS` a row is marked `failed` and stays visible. Notifications are
**advisory** and carry only `event.v1` fields — never an operator name. Watch the
outbox at `GET /sites/{id}/notifications?state=failed` (supervisor+).

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
| Operational annotations | `MM_RETAIN_ANNOTATIONS_DAYS` | 365 |
| Audit trail | `MM_RETAIN_AUDIT_DAYS` | 730 |

Operational annotations are delay classifications, shift handovers, and **closed**
incidents (with their notes). An open or in-progress incident is live work and is
never age-deleted regardless of the window — only its close time starts the clock.

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

## 7. Devices, broker auth and MQTT provisioning

The broker is the transport-layer authority. Internal clients (the ingestor consumer
and the simulator / Teltonika-adapter publishers) authenticate as one **service
account** (`MM_MQTT_USERNAME` / `MM_MQTT_PASSWORD`); each native-MQTT tracker
authenticates with its `device_id` and is confined by ACL to writing only its own
`mm/<site>/<asset>/position` topic.

**Dev/demo default is anonymous** (`docker/mosquitto.conf`, `allow_anonymous true`) so
a fresh install runs with no setup. **On a real site, set the service account** —
`deploy.sh` then generates the broker credential files, selects the hardened config
(`docker/mosquitto.auth.conf`, `allow_anonymous false`), and enforces auth. With
`MM_ENV=prod` the API refuses to boot if the broker would be anonymous.

```bash
# Provision a device (admin, audited). A broker secret is returned ONCE — configure
# the tracker with it; only its hash is stored, so it cannot be retrieved later.
POST /sites/{site_id}/devices  { "device_id": "trk-1", "asset_id": "HT-102", "source": "teltonika" }
# Reissue a lost/rotated secret (returned once):
POST /sites/{site_id}/devices/{device_id}/rotate-secret
# Enable/disable (a disabled device drops out of the password + ACL files):
POST /sites/{site_id}/devices/{device_id}/enabled  { "enabled": false }
```

One asset binds to one device (a second binding returns 409). **Regenerate and reload
the broker whenever devices change** — this rewrites both the password file (issued
device secrets) and the ACL (per-device topic):

```bash
docker compose exec api uv run python -m minemonitor.devices.broker_config
docker compose exec mqtt kill -HUP 1
```

**Strict mode.** With `MM_MQTT_REQUIRE_REGISTERED_DEVICE=true` the ingestor also accepts
telemetry only for assets with an enabled device row — a second, source-agnostic check
above the broker ACL. It is **off by default** so a fresh or demo install ingests
without provisioning. Independently of this flag, the ingestor always rejects any
message whose topic and payload disagree (anti-spoof).

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
