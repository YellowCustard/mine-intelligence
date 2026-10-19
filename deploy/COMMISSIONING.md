# Mine Monitor — Site Commissioning & Acceptance Runbook

The exact, ordered procedure to take Mine Monitor from a clean on-site server to a
production-accepted deployment with a real GNSS tracker. Every step is a command you
run or an observation you record — there are no undocumented manual steps.

Fill in the **Result** column as you go; the completed table is the acceptance record.

- **Timezone:** the site renders in `Africa/Harare` (UTC+2). Timestamps are stored UTC.
- **Convention:** a Teltonika tracker's **IMEI is its `device_id`**. Provision the
  device with `device_id = <IMEI>`; the TCP listener routes it to that device's
  `(site_id, asset_id)` and refuses unknown/disabled IMEIs when strict mode is on.
- **Advisory only:** every output warns a person; nothing here actuates plant.

---

## Stage A — Server bring-up

Run on the clean on-site server, from the repo root.

| # | Action | Command | Expected | Result |
|---|---|---|---|---|
| A1 | Install Docker Engine + compose plugin | (distro package) | `docker compose version` prints a version; `docker version --format '{{.Server.Version}}'` is 28.0 or later (older engines can let a host on the same LAN reach container ports directly) | |
| A2 | Configure environment | `cp .env.example .env` then edit | strong `POSTGRES_PASSWORD` (plus `MM_OBJECTSTORE_*` and `MM_EDGE_S3_*` only if the `evidence` profile is used); set `MM_ENV=prod`, `MM_DEFAULT_SITE_TZ=Africa/Harare`, `MM_DEFAULT_SITE_ID`, and `MM_MQTT_USERNAME`/`MM_MQTT_PASSWORD` (prod refuses an anonymous broker) | |
| A3 | Bring up the stack (writes the broker password and ACL files when `MM_MQTT_USERNAME` is set) | `bash deploy/deploy.sh` | prints `Broker refuses anonymous clients.`, waits for the API to be healthy, writes `backups/minemonitor_<time>_after-deploy.sql.gz` and prints next steps; it exits with an error if the broker lets a client in with no login or the dump fails | |
| A4 | Enable TLS | `docker compose --profile tls up -d` | Caddy on 443; internal CA on-prem, or Let's Encrypt for a public domain (see DEPLOY.md) | |
| A5 | Confirm migrations ran | `docker compose exec api uv run alembic current` | head is the latest revision | |
| A6 | Create the first admin | `docker compose exec api uv run python -m minemonitor.auth.cli <user> admin` | user created | |
| A7 | Post-deploy smoke test | `MM_SMOKE_URL=… MM_SMOKE_USER=… MM_SMOKE_PASSWORD=… MM_SMOKE_SITE=… docker compose exec -T api uv run python -m minemonitor.smoke` | 6/6 checks pass, exit 0 | |
| A8 | Exposure | on the server `docker compose ps`; then the probes below, each **from another machine** | 5432 and 8000, always: `127.0.0.1` in PORTS and `closed` from every probe. 1883 and 8333: the same, unless the site set `MM_MQTT_BIND` (device-VLAN address, broker auth on) or `MM_OBJECTSTORE_BIND` (LAN address, for a vision edge node on another box); then PORTS shows that address, the probe of that address from the network it serves prints `OPEN`, and every other probe of that port prints `closed`. 5027 (`trackers` profile): PORTS shows the `MM_TELTONIKA_BIND` address; the outside probe prints `closed`; the LAN probe prints `closed` unless tracker traffic arrives on the box's LAN address (a private APN routed to it); D1 proves the trackers reach it | |

**A8 probes.** `ufw` does not cover these ports: Docker forwards published ports through its own
iptables rules, which `ufw`'s rules never see. Only the `MM_*_BIND` addresses in `.env` keep a port
off a network, so check from other machines rather than reading the firewall. A port answers only on
the address PORTS shows, so each probe targets the address a given network would use. Define this
in a shell on the probing machine; a refused or filtered port both print `closed`:

```bash
probe() { local h=$1 p; shift; for p in "$@"; do
  if timeout 3 bash -c "</dev/tcp/$h/$p" 2>/dev/null; then echo "OPEN   $h:$p"; else echo "closed $h:$p"; fi
done; }
```

| From | Run |
|---|---|
| a machine on the mine LAN | `probe <box LAN address> 5432 1883 8000 8333 5027` |
| outside the site, if it has a public address (the box's own, or the router's if it forwards ports) | `probe <public address> 5432 1883 8000 8333 5027` |
| a host on the device VLAN, only if `MM_MQTT_BIND` is set | `probe <MM_MQTT_BIND> 1883` |
| a host on the LAN, only if `MM_OBJECTSTORE_BIND` is set to an address other than the box's LAN address | `probe <MM_OBJECTSTORE_BIND> 8333` |

80/443 (the `tls` profile) are meant to answer. A tracker cannot run the probe, so D1 is the check
that 5027 answers on the tracker network.

## Stage B — Site configuration

| # | Action | How | Expected | Result |
|---|---|---|---|---|
| B1 | Confirm site row | dashboard / API | site present with correct tz | |
| B2 | Define shifts | `PUT /sites/{id}/shift-definitions/{name}` (admin) | day/night (or site pattern) resolve; cross-midnight correct | |
| B3 | Create zones | zones API | load face, ROM pad, dump, restricted magazine, speed-limited — polygons match the survey | |
| B4 | Set zone rules | zone `rules` payload | restricted/overspeed/dwell thresholds per zone | |
| B5 | Create the asset | assets | the machine the first tracker will ride | |
| B6 | Confirm the OSH registers live elsewhere | ask the mine's SHEQ lead | the mine names where it keeps its accident and disease registers (Occupational Safety and Health Act 7 of 2026, s.22: kept 50 years); Mine Monitor is not that register (`docs/OPERATIONS.md` §6) | |

## Stage C — First tracker: provisioning & installation

Complete the Stage K firmware and isolation rows for each device before connecting it.

| # | Action | How | Expected | Result |
|---|---|---|---|---|
| C1 | Physical install | on the machine | power, ground, ignition sense wired; GNSS + cellular antennas mounted with sky view | |
| C2 | Provision the device | `POST /sites/{id}/devices` with `{"device_id":"<IMEI>","asset_id":"<asset>","source":"teltonika"}` | 201; a broker `secret` is returned **once** (only needed if the tracker also uses MQTT — raw-TCP Teltonika authenticates by IMEI) | |
| C3 | Confirm enabled | `GET /sites/{id}/devices` | device present, `enabled: true` | |
| C4 | Point the tracker at the edge server | tracker config (SIM APN, server host, **port 5027**, protocol TCP, Codec 8 or 8E) | tracker configured | |
| C5 | Strict mode (recommended for production) | `.env` `MM_MQTT_REQUIRE_REGISTERED_DEVICE=true`, restart ingestor + teltonika | only provisioned IMEIs accepted | |
| C6 | TLS on the tracker link (recommended) | PEM certificate and key in `var/teltonika-tls/`; `.env` `MM_TELTONIKA_TLS_CERT=/run/teltonika-tls/<cert>` and `MM_TELTONIKA_TLS_KEY=/run/teltonika-tls/<key>`; restart teltonika. On each tracker, switch the server link to TLS per Teltonika's guide for the model (FMB firmware added TLS in 03.27.07); confirm on the first unit before the rest | `teltonika listener up` logs `"tls": true`; D1 passes over TLS; a tracker left on plain TCP is not accepted (switch them all) | |

## Stage D — Connectivity & telemetry

| # | Action | How | Expected | Result |
|---|---|---|---|---|
| D1 | Tracker connects | `docker compose logs -f teltonika` | `teltonika connected` with the **correct site_id/asset_id** (not `teltonika-<imei>`) | |
| D2 | Unknown IMEI is refused | (temporarily point an unprovisioned tracker, strict on) | handshake rejected; no data stored | |
| D3 | Telemetry lands | `GET /sites/{id}/positions?asset_id=<asset>` | lat, lon, ts, speed, heading, ignition, satellites present and plausible | |
| D4 | `ts` vs `received_at` | inspect a position | device time vs server time both present; gap small on a live link | |

## Stage E — Dashboard

| # | Action | Expected | Result |
|---|---|---|---|
| E1 | Marker on the map | correct asset, correct position, live update without refresh (SSE) | |
| E2 | Equipment state | moving/idle/stopped derived; **stopped ≠ offline**; `basis` shows observed vs inferred | |
| E3 | Timestamp & units | Harare local time; `speed_kph` etc. correct | |

## Stage F — Geofence (physical drive test)

Drive the vehicle through a test zone.

| # | Action | Expected | Result |
|---|---|---|---|
| F1 | Enter the zone | one `zone_entry` after the debounce fixes — **no storm** on the boundary | |
| F2 | Dwell / speed | dwell or overspeed event only if the rule threshold is actually breached | |
| F3 | Exit the zone | one `zone_exit` after the hysteresis buffer | |
| F4 | Restricted entry | driving an unauthorised class into the magazine raises exactly **one** critical alarm | |
| F5 | Acknowledge | ack the alarm in the dashboard; state open→acknowledged | |

## Stage G — Communications failure & backfill

| # | Action | Expected | Result |
|---|---|---|---|
| G1 | Disconnect cellular/antenna for several minutes | tracker buffers locally; `asset_offline` after `MM_OFFLINE_THRESHOLD_S`, **distinguished from stopped** | |
| G2 | Restore comms | tracker reconnects (new handshake) and backfills | |
| G3 | Verify no loss / no duplicates | the buffered fixes appear with their original `ts`; counts are correct (ingest is idempotent) | |
| G4 | Cycle integrity | a haul cycle spanning the outage is recomputed correctly from stored positions | |

---

## Stage H — Failure injection (§7)

Record for each: **expected · actual · data lost? · duplicated? · recovery time · operator-visible impact.**

| Target | Test | Expected |
|---|---|---|
| Tracker | disconnect / reconnect | offline event, then backfill; no loss/dupes |
| Tracker | invalid/unprovisioned IMEI (strict) | handshake refused |
| Tracker | duplicate / out-of-order fixes | deduped; late fixes stored, skipped for live eval, available to recompute |
| Broker | `docker compose restart mqtt` | publisher spool holds; ingestor reconnects; no loss |
| Broker | wrong `MM_MQTT_PASSWORD` | ingestor/publisher fail to connect **loudly**; no silent drop |
| API | `docker compose restart api` | dashboard reconnects (SSE); no data path affected |
| Database | `docker compose restart db` | ingestor withholds ack until durable; **restart loses nothing, duplicates nothing** |
| Server | `docker restart` / host reboot / unclean power cut | `restart: unless-stopped` brings the stack back; spool + WAL intact |
| Network | LAN / internet / cellular interruption | local-first operation continues; backfill on recovery |

## Stage I — Backup & restore rehearsal (§17)

Run against the **actual Docker deployment**, not a bare database.

| # | Action | Command | Expected | Result |
|---|---|---|---|---|
| I1 | Take a backup | `bash deploy/backup.sh` | `.sql.gz` written; `gunzip -t` passes | |
| I2 | Note baseline | count rows / note alembic head | recorded | |
| I3 | Restore onto a clean DB | `bash deploy/restore.sh <dump>` | brings up db-only, loads, starts the stack | |
| I4 | Verify head + data | `alembic current`; row counts | match baseline exactly | |
| I5 | App runs | `curl /health`; smoke test | green; login works (password hashes survived) | |

> The dump→restore **data-integrity** path is already proven in CI/local against real
> PostgreSQL 16; this stage proves it through the real `db` **container**.

## Stage J — Soak test (§18)

Run continuously **≥ 24 h (prefer 72 h)** with the tracker(s) live or the simulator.

Sample hourly and watch for drift/leaks:

- CPU, RAM, disk free, database size
- telemetry volume, ingest backlog, API p95 latency
- broker stability, device connection count
- background jobs (retention, recompute, notification dispatch) firing on schedule
- backup executed and pruned (`--profile backup`)
- log volume and any repeated errors

**Pass:** flat resource curves, no unbounded growth, `/health` green throughout,
`/sites/{id}/system-health` and `/data-quality` clean.

## Stage K — Field-device firmware & isolation

Run before a gate terminal, generator gateway or tracker is connected, alongside Stage C. A
row marked *(if used)* applies only when that device is on site. Each floor comes from a vendor
advisory; a device below it is updated, or replaced, before it is connected. Use the A8 `probe`
function for the reachability checks.

| # | Action | How | Expected | Result |
|---|---|---|---|---|
| K1 | Dahua gate terminal (ASI) firmware | read the build date on each terminal's version/system-info page | build dated **2026-03-26 or later**. Older builds have CVE-2026-29116 (Dahua DHCC-SA-202606-001): an unauthenticated, crafted request makes the terminal throw an exception and reboot. A researcher's disclosure of the same flaw ([Full Disclosure, 2026-08-27](https://seclists.org/fulldisclosure/2026/Aug/120)) adds that a device without a watchdog stays down until someone recovers it on site | |
| K2 | Dahua NVR and IP cameras *(if used)* | as K1, per device | NVR built **2026-03-26 or later** (CVE-2026-29116); IP cameras built **2026-04-15 or later** (CVE-2026-29114, same advisory) | |
| K3 | Gate network isolation | gate terminals on their own VLAN. First read the firewall or switch ACL in front of that VLAN: the only rule allowing traffic to a terminal's API port (the host and port in `MM_ACCESS_GATE_URL`) has the Mine Monitor box's address as its source. Then `probe <terminal> <API port>` from the Mine Monitor box, and from one host on every other network that routes to the gate VLAN (the mine LAN, the device VLAN, office or Wi-Fi networks, the gate VLAN itself if it has other hosts) | the ACL allows only the Mine Monitor box; the probe prints `OPEN` from the box and `closed` from every other network. Nothing but the Mine Monitor host can reach a terminal's API. Do not record a pass on probes alone: a probe samples one host, the ACL is what excludes the rest | |
| K4 | Gate terminal clock | NTP configured on each terminal; compare its clock with the server's | within 2 minutes of the server (`MM_MAX_FUTURE_SKEW_S`, 120 s). A terminal clock that runs ahead stops its gate events being recorded | |
| K5 | DSE855 generator gateway *(if used)* | own VLAN. Read the ACL in front of it as in K3: the only allow rule is TCP 502 from the Mine Monitor box. Then `probe <DSE855> 502 80 443` from the Mine Monitor box, and from one host on every other network that routes to that VLAN | the ACL allows only TCP 502 from the Mine Monitor box; from the box only 502 is `OPEN` (80 and 443 `closed`); from every other network 502, 80 and 443 are all `closed`. Firmware is the latest from the vendor (1.2.3 as of Oct 2026) and at least 1.2.0 (CVE-2024-5947). Firmware alone is **not** enough: CVE-2024-5948 and CVE-2024-5950 (unauthenticated code execution from the same network) and CVE-2025-29270 have no fixed version, so isolation is the control. Do not record a pass on probes alone | |
| K6 | Teltonika FMB trackers | read the firmware version (Configurator, or the `getver` SMS/GPRS command), and the model | at or above the factory firmware Teltonika's [Firmware versions](https://wiki.teltonika-gps.com/view/Firmware_versions) page lists for that model. As of Oct 2026: FMB920, FMB120, FMB125 and FMB130 **04.00.00.Rev.13**; FMB140 **04.02.00.Rev.05**; FMB122 **03.29.00.Rev.21**. A unit on 04.00.00 cannot be downgraded | |
| K7 | Teltonika RUT/TSW routers *(if used for backhaul)* | read the firmware version in the router's or switch's web UI | RutOS **7.24.2 or later** (CVE-2026-18368, CVE-2026-16455); TSW switches TSWOS **1.10.1 or later** (CVE-2026-16455) | |

Re-check K1, K2 and K5 against the vendors' advisory pages when a device is added or replaced.
FIRST lists Dahua's PSIRT as suspended, so read Dahua's advisory index directly rather than
waiting for a notice.

---

## Acceptance sign-off

Production acceptance = **A–I and K all pass** and **J shows no degradation**. Record the
operator, date, tracker IMEI/asset, and any deviations. File the completed tables with
the release record and the [Release Readiness report](../docs/) alongside
`RELEASE_CHECKLIST.md`.
