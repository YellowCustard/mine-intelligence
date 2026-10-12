# Mine Monitor — Operations Manual

For the person running a deployed Mine Monitor instance: bringing it up, keeping it
healthy, and recovering it. Deployment mechanics live in
[`deploy/DEPLOY.md`](../deploy/DEPLOY.md); the release gate is in
[`deploy/RELEASE_CHECKLIST.md`](../deploy/RELEASE_CHECKLIST.md). This document is the
day-to-day and 2am-from-another-country runbook.

All times are stored in UTC and rendered in the site timezone
(`Africa/Harare`, UTC+2). Shifts cross midnight — a day boundary is not a shift
boundary. Every time sent to the API or published on a contract must carry its offset
(`2026-09-05T11:42:07Z` or `…T13:42:07+02:00`); one without (`2026-09-05T11:42:07`) is
refused with 422, because it could mean either and would otherwise be stored two hours off.

---

## 1. What is running

One `docker compose` stack:

| Service | Role | Loss of it means |
|---|---|---|
| `db` | PostgreSQL 16 + TimescaleDB | Everything stops. The only stateful service that must be backed up. |
| `mqtt` | Mosquitto broker | New telemetry queues on the device spools; nothing is lost, backfilled on recovery. |
| `api` | FastAPI + dashboard + SSE | Dashboard and API down; ingest via MQTT continues into the ingestor. |
| `ingestor` | MQTT subscriber, rules, offline check, retention | No new events/metrics; positions still spool device-side. |
| `objectstore` | S3 object store for vision evidence (SeaweedFS, `--profile evidence`) | No effect in Phase 1; edge nodes in `s3` evidence mode keep stills on the node and retry. |
| `simulator` | Synthetic fleet (`--profile sim`) | Demo/test only; never in production. |

The API serves the dashboard on port **8000, bound to localhost**. HTTP Basic
credentials must ride TLS — never expose plain 8000 publicly. Reach it with an SSH
tunnel (`ssh -L 8000:127.0.0.1:8000 <user>@<host>`) or a TLS reverse proxy.

---

## 2. Bring it up

**Installed from a release** ([`deploy/INSTALL.md`](../deploy/INSTALL.md)): `sudo ./install.sh`
from the bundle installs or upgrades, and `sudo mmctl` does the rest (`status`, `deploy`,
`backup`, `restore`, `rollback`, `logs`). Every `docker compose ...` command in this manual is
`sudo mmctl compose ...` there, and `bash deploy/backup.sh` / `restore.sh` are `sudo mmctl backup`
/ `restore`.

**From a git checkout**, on the server, from the repo root:

```bash
bash deploy/deploy.sh          # core stack
bash deploy/deploy.sh --demo   # + simulated fleet (non-production only)
```

The script is idempotent — re-run it to apply an update. It refuses to continue
without a `.env` (it copies `.env.example` and stops so you can set real passwords).
Every deploy ends with a database dump, `backups/minemonitor_<UTC time>_after-deploy.sql.gz`
(§5), and fails if that dump does; `--no-backup` skips it.

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
signal), the ingestor raises one `zone_occupancy` `event.v1` into the same queue for as
long as the zone stays over its cap. When a tick finds the zone back at or under its cap, the
alarm is marked `cleared_at`, and the next breach raises a new alarm whether or not the first
was acknowledged. It is a cross-asset aggregate checked on the maintenance tick,
source-agnostic (GNSS today, vision tracks later), and advisory. `asset_offline` and
`camera_offline` are deduped the same way: one alarm per outage, and a new outage after the
asset or camera reports again is a new alarm.

Set or clear a sector's capacity without resending the whole zone via
`PUT /api/v1/sites/{id}/zones/{zone_id}/occupancy` (admin, audited) with
`{"max_occupancy": N, "severity": "warning|critical"}` — a null `max_occupancy` clears the
cap and opts the zone out. Live occupancy vs capacity per capped sector is at
`GET /api/v1/sites/{id}/zones/occupancy` (viewer), and the dashboard's **Zone occupancy**
panel renders it (over-capacity sectors flagged), alongside the alarm in the queue.

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

**Generator monitoring (FP-11).** DSE genset controller readings (engine hours, running state,
fuel level, fault) are ingested per generator (`POST /api/v1/sites/{id}/generators/{asset_id}/
readings`, supervisor, audited) — generators are `assets` with `asset_class="generator"`. The
data is **measured** controller output, not a fabricated sensor; a value the controller does not
report is null, never invented. A reading with a `fault` raises a critical `generator_fault`
alarm; one that is `running` while `expected_running` is false raises a `generator_unexpected_run`
warning (both deduped per reading, both advisory — the platform never controls a genset).
Run-hours **health** reuses the deterministic maintenance indicator over the latest reported
engine hours (`GET .../generators/{asset_id}/health`; `Unknown` until a plan + baseline exist).
Simulator-first (`ingest/adapters/dse_sim.py`); the live DSE **Modbus** poller and register map
(`MM_GENERATOR_MODBUS_*`) are confirmed per DSE model on site before go-live.

**Gold reconciliation (FP-09, groundwork).** Measured material quantities through the circuit
(ore→crusher→mill→tanks→carbon→furnace→output→gate) are ingested as `material.measurement.v1`
(`POST /api/v1/sites/{id}/reconciliation/measurements`, supervisor, audited; list at the same
path, viewer). Each carries its stage, unit, `source`, an optional `assay_ref` (→ FP-08 lab), and
`provenance: measured`; ingest is idempotent on a deterministic id (per source ref), so
re-imports never double-count production. **This is ingestion only** — the platform records the
measured facts; it does **not** yet compute an expectation, a variance, or a theft conclusion
(those slices are gated on weightometer feeds + baselined historical variation). The conclusion
is always a human's: the system will flag *unexplained variance*, never assert theft.

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

The database is the only thing that must be backed up, with one exception: if the `evidence`
profile runs with `MM_EDGE_EVIDENCE=s3`, its stills live in the `objectstore_data` volume, which
the commands below do not cover. Decide with the mine whether to back that volume up or accept
losing stills under the evidence retention before `s3` mode goes live; see
[Evidence object storage](OBJECT_STORAGE.md).

```bash
bash deploy/backup.sh                       # -> backups/minemonitor_<timestamp>.sql.gz
bash deploy/backup.sh /path/to/out.sql.gz   # explicit path
```

`backup.sh` writes to a `<file>.partial.*` file and renames it only once `pg_dump` has finished, so
a file under a dump's name is always a whole dump; a failed dump exits non-zero and leaves
nothing. Dumps hold personal data: they are written readable by their owner only, and
`backups/` is kept at 0700 (so restoring a dump the root-run backup service wrote needs `sudo`). It
refuses to start when the disk could not take another dump the size of the largest one already
in the directory, plus a fifth, with 1 GB left free for the database: copy old dumps off the box.

**After every deploy.** `deploy.sh` ends with a dump (`_after-deploy`), and on a release install
`mmctl` takes one after every install, upgrade, `mmctl deploy` and rollback once the release has
verified (`_v<version>`, `_v<version>-installed`, `_v<version>-rolled-back`). A deploy whose dump
fails reports an error although the stack is up: it has no dump yet that restores onto it. These
dumps sit in the same directory as the scheduled ones and are pruned with them.

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
then starts the API. With broker auth on it writes the broker files from the restored
devices table, then brings up the rest of the stack. Verify with `docker compose ps` and
`curl -fsS http://127.0.0.1:8000/health`, then run the smoke test (§3).

**TimescaleDB upgrades.** When the `db` image moves to a new TimescaleDB release, `deploy.sh`
runs `ALTER EXTENSION timescaledb UPDATE` before the API starts, prints
`Updated the TimescaleDB extension to <version>`, and closes the sessions still on the old
version so the API and ingestor reconnect, as after a database restart. A dump restores only
into the TimescaleDB version it was taken on
([Timescale](https://github.com/timescale/docs/blob/latest/_partials/_migrate_from_timescaledb_version.md)),
which is why every deploy ends with a fresh dump (above). To restore an older dump,
check out the release it was taken on, restore it there, then check out the current release
and run `deploy.sh`.

---

## 6. Data retention and erasure (CDPA / SI 155 of 2024)

The mine is the data controller; eigenstate is the processor. Retention is
**configurable per data class**, in days, in `.env`; a scheduled deletion job runs on
`MM_RETENTION_INTERVAL_S` (≈ daily). `0` = keep forever.

| Class | Variable | Default |
|---|---|---|
| Raw positions | `MM_RETAIN_POSITIONS_DAYS` | 90 |
| Derived metrics + cycles | `MM_RETAIN_METRICS_DAYS` | 365 (kept after their raw positions are deleted) |
| Events | `MM_RETAIN_EVENTS_DAYS` | 365 |
| Operational annotations | `MM_RETAIN_ANNOTATIONS_DAYS` | 365 |
| Audit trail, and closed breach records | `MM_RETAIN_AUDIT_DAYS` | 730 |

Operational annotations are delay classifications, shift handovers, and **closed**
incidents (with their notes). An open or in-progress incident is live work and is
never age-deleted regardless of the window — only its close time starts the clock.
The same holds for breach records: an open breach is never deleted, and a closed one
follows the audit window from its close.

Force a retention pass now: `POST /admin/retention/run` (admin).

**Personal-data requests.** Operator identity lives only in the `operators` table
(events reference it by foreign key, never by name). Export or erase per operator via
the operators endpoints; erasure is a tombstone that leaves history intact. There are
**no biometric templates** in this database — the gate face terminal yields only an
access event, never an image or template.

**Personal-data breaches.** The reported obligation is notice to the regulator
(POTRAZ) within 24 h and to the people affected within 72 h. When personal data held
here is lost, exposed or altered, an admin records it in the breach register as soon
as it is known; the platform then keeps both deadlines in front of the control room.
Use the **audit log** (access to personal data, rule and zone changes) to establish
scope.

1. **Record it**: `POST /sites/{site}/breaches` with `discovered_at`, optionally
   `occurred_at`, a `summary`, the `data_classes` touched (`operator_records`,
   `positions`, `access_events`, `events`, `audit_log`, `evidence`, `other`), the
   `operator_ids` of the people affected, and `notify_individuals` (`false` only when
   notice to them is assessed as not required). Both deadlines are fixed now and
   returned.
2. **Watch the alarms**: each unsent notice raises a `breach_notice_due` **warning**
   `MM_BREACH_WARN_BEFORE_H` (6) hours before it is due and a **critical** alarm once
   it is overdue, in the normal alarm queue and through any configured notification
   channel. An alarm names the breach id and the deadline only, never the summary or
   the people.
3. **Tell the people affected**: `GET /sites/{site}/breaches/{id}/affected.csv` gives
   their names and contacts (erased people appear without them). The download is
   audited as a personal-data export.
4. **Record each notice**: `POST /sites/{site}/breaches/{id}/notices/regulator` (with
   `notified_at` and POTRAZ's `reference`) and `…/notices/individuals`. This stops that
   notice's alarms and acknowledges the ones already raised.
5. **Close it**: `POST /sites/{site}/breaches/{id}/close` with a `note` (also for a
   report that turns out not to be a breach). Any notice still unsent is recorded in
   the audit entry.

`GET /sites/{site}/breaches` lists open breaches with each notice's status
(`pending`, `due_soon`, `overdue`, `sent`, `not_required`, `not_sent`);
`?state=all` includes closed ones. All of it is admin-only and audited.

The deadlines are settings, because the legal position is still being confirmed:
`MM_BREACH_REGULATOR_NOTICE_H` (24), `MM_BREACH_INDIVIDUALS_NOTICE_H` (72),
`MM_BREACH_DEADLINE_FROM` (`discovered`, or `occurred` when the occurrence time is
known) and `MM_BREACH_REGULATOR_NAME` (POTRAZ). A breach keeps the deadlines it was
recorded with, so changing them affects only breaches recorded afterwards.

---

## 7. Devices, broker auth and MQTT provisioning

The broker is the transport-layer authority. Internal clients (the ingestor consumer
and the simulator / Teltonika-adapter publishers) authenticate as one **service
account** (`MM_MQTT_USERNAME` / `MM_MQTT_PASSWORD`); each native-MQTT tracker
authenticates with its `device_id` and is confined by ACL to writing only its own
`mm/<site>/<asset>/position` topic.

**Dev/demo default is anonymous** (`docker/mosquitto.conf`, `allow_anonymous true`) so
a fresh install runs with no setup. **On a real site, set the service account** in
`.env`. Compose then runs the hardened config (`docker/mosquitto.auth.conf`,
`allow_anonymous false`) on every `docker compose up`, unless `MM_MOSQUITTO_CONF` is set: that
override wins over the login, so leave it unset on a site. `deploy.sh` writes the broker
credential files the hardened config needs and fails unless the broker refuses a client with no
login, which also catches an override that leaves the broker open.
Without those files the hardened broker does not start, so it never falls back to open. With
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
above the broker ACL. HTTP ingest applies the same check to `device` accounts (an admin is
exempt, so the post-deploy smoke check still runs). It is **off by default** so a fresh or
demo install ingests without provisioning. Independently of this flag, the ingestor always
rejects any message whose topic and payload disagree (anti-spoof).

**HTTP ingest uses the same binding.** A `device` account whose username is a provisioned
`device_id` is the identity the broker knows, so `POST /ingest/positions` and
`POST /ingest/vision` hold it to what the ACL lets that device write: a tracker posts only
its own asset's positions, a vision edge node only its own site's vision records, and a
disabled device nothing (`403` with the reason). Name a tracker's or edge node's HTTP account
after its `device_id`. A `device` account with no device row (a gateway, the shared ingest
account) keeps its site scope only.

### 7.1 First-party vision edge nodes (Stage B)

A vision edge node is a **`device`** principal, exactly like a tracker — no new auth surface.
It publishes thin `vision.*.v1` records (operational events, 5-min zone rollups, node/camera health)
either to `POST /api/v1/ingest/vision` (one record or a batch of up to 500) or over MQTT on
`<prefix>/<site>/vision/<camera_or_node>/<kind>`. Raw frames never leave the node.

- **Before a node can raise alarms**, an admin installs the rule set once per site:
  `POST /api/v1/sites/<site>/vision/rules/defaults` (audited). Rules are immutable versions —
  change one by publishing a new version (`POST .../vision/rules`) and retire the old
  (`POST .../vision/rules/<rule_id>/<version>/retire`). An operational event citing an unknown rule
  version is refused.
- **The node pulls its config** from `GET /api/v1/sites/<site>/vision/config` (scene zones, zone
  capacity, active rules) and caches it, so it keeps working through a link outage. Stream URLs are
  never served — the node holds its own camera credentials.
- **Batches are all-or-nothing.** A malformed record, an unregistered camera, or an uncited rule
  rejects the whole batch with `422` and the failing `index`; nothing is partially stored. Replays
  are idempotent (`duplicates` in the response).
- **Reading it:** vision alarms land in the normal queue with `source = vision:<camera_id>` and a
  provenance label in `detail`. `GET .../vision/events` (the evidenced conclusions),
  `.../vision/rollups` (people counts per zone) and `.../vision/health` (latest per node/camera).
- **Missed lane:** a commissioned camera whose health goes silent for `MM_VISION_CAMERA_STALE_S`
  (default 300 s) — or whose node reports that no frames arrive for that long — raises one
  `camera_offline` alarm per camera. A frozen picture is shown as a condition, not alarmed.
- **Running the edge node:** `docker compose --profile vision up -d vision-edge` with the
  `MM_EDGE_*` settings in `.env` (camera stream URLs live only there). It is healthy while its
  heartbeat is fresh; `GET .../vision/health` shows node status (`degraded` while a camera is down
  or it runs from its cached config, `down` after a clean stop). Commissioning steps and the
  on-site acceptance walk-through: `docs/VISION_DEPLOYMENT.md` §8.
- **Scene setup (per camera, admin, audited):** `PUT /api/v1/sites/<site>/cameras/<cam>/scene`
  with `image_zones` (up to 16 normalised polygons in image coordinates, each mapped to an
  existing `zone_id` + `zone_version`) and, optionally, a `homography`. Supplying a homography
  marks the camera `calibrated`; `"homography": null` clears it. Without one, nothing about
  distance is claimed. A bad scene is rejected whole (`422`); the node picks up the change on its
  next config pull.
- **Live scene:** `GET .../vision/scene` — per camera: condition (ok/dark/defocused/obstructed/
  no_signal), people in view, people per zone, open alarms, and `stale` when the latest rollup is
  older than two buckets. The Fleet view's **Vision** panel shows the same.
- **Shift report:** `GET .../vision/report` (`?shift_id=`, `?at=`, or `?since=&until=`; default
  the current shift) and `.../vision/report.csv`. Per camera × zone: peak and time-weighted mean
  people, entries, exits, longest dwell, restricted intrusions, and `coverage_pct`. These are
  observed numbers only, with no targets. Low coverage means the camera could not see for that
  part of the shift, so treat zero people under low coverage as "unknown", not "empty".

---

## 8. Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `/health` degraded, `/healthz` OK | Ingestor heartbeat stale or MQTT unreachable. `docker compose logs ingestor mqtt`; check `MM_HEARTBEAT_STALE_S`. |
| System health lists `failing_steps` | The ingestor is up but one maintenance step (for example `cycles`, `offline`, `retention`) failed on its latest run; the others still ran. `docker compose logs ingestor` and search for `maintenance step failed`: each line names the step, the site and the error. The step is retried every tick and drops off the list once it succeeds. |
| A second site gets no alarms or cycles | The ingestor serves every site in the `sites` table plus `MM_DEFAULT_SITE_ID`. There is no API to add a site yet: insert its row (`INSERT INTO sites (site_id, name, timezone) VALUES (...)`) before its devices report. The one gate feed (`MM_ACCESS_GATE_URL`) belongs to `MM_DEFAULT_SITE_ID`. |
| API won't start under `MM_ENV=prod` | The production guard rejected sample DB credentials or a weak bootstrap password. Set real secrets in `.env`. The log line names the offending value. |
| Alarm storm on a boundary-hugging asset | Debounce/hysteresis too tight for local GNSS quality. Widen the per-zone enter-fix count / exit buffer (defaults: 2 fixes in, 15 m out). |
| Positions stop arriving | Broker or link down — devices spool locally and backfill on recovery; nothing is lost. Confirm with the device spool and `received_at` vs `ts` gaps. |
| Late/out-of-order data changed a cycle | Expected and handled automatically. Ingest marks a fix that lands behind the last recompute, and the next maintenance tick rebuilds that asset's cycles and buckets from far enough back to cover it (at most `MM_CYCLE_MAX_S` plus a few minutes). The result is identical to a full rebuild. |
| Zone edited, or the cycle logic fixed, and history should reflect it | The tick applies a zone edit from the next run onwards. To rewrite history, `POST /sites/{id}/recompute` (admin), optionally with `?since=`; it rebuilds everything stored positions still back, and keeps older cycles and buckets as they are. It works through history in 12-hour slices, so live ingest and alarms carry on while it runs. |
| A very long "cycle" is missing from the chart | A load-to-load span longer than `MM_CYCLE_MAX_S` (default 4 h) is a breakdown or an overnight park, not haulage, and is not reported. Raise it if real hauls at the site take longer. |
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
