# Deploying Mine Monitor to a VPS (xneelo Cloud, or any Docker host)

The dashboard is not a standalone file — it is served by the API, which needs the
whole stack (PostgreSQL/TimescaleDB, Mosquitto, the API and the ingestor, plus the
simulator for demo data until real trackers are on site). So it runs on a
**Docker-capable server**, not xneelo *shared* hosting.

> **On xneelo:** use a **Cloud/VPS** instance (an Ubuntu server you get root on),
> not the shared "Web Hosting" product. Shared hosting cannot run Docker.

## 1. Prerequisites (once, on the VPS)

- Ubuntu 22.04+ (or similar), **2 GB RAM minimum** (4 GB comfortable), ~10 GB disk.
- Docker Engine + the Compose plugin:
  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"   # log out/in so this takes effect
  ```

## 2. Get the code

```bash
git clone https://github.com/YellowCustard/mine-intelligence.git
cd mine-intelligence
```

(To update later: `git pull`, then re-run the deploy script.)

## 3. Configure secrets

```bash
cp .env.example .env
# EDIT .env — at minimum change:
#   POSTGRES_PASSWORD    (database password)
#   MINIO_ROOT_PASSWORD  (object-store password)
# And set a first-run admin so the dashboard is reachable on first boot:
#   MM_BOOTSTRAP_ADMIN_USER=admin
#   MM_BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
```

The bootstrap admin is created **only if the users table is empty**. Once you
have logged in and created real users (below), blank both values and redeploy so
no password lives in `.env`.

## 4. Bring it up

```bash
bash deploy/deploy.sh --demo     # core stack + the simulated fleet (demo data)
# or:  bash deploy/deploy.sh     # core stack only (for real trackers later)
```

This builds the image, starts `db`, `mqtt`, `minio`, `api` and `ingestor` (the API
runs the Alembic migrations on start), and — with `--demo` — the `simulator`.

The API (and the dashboard at `/`) listens on **127.0.0.1:8000** on the VPS.

## 4a. Create users (M6)

Log in as the bootstrap admin, then create real accounts. Roles are a hierarchy
(`viewer` < `supervisor` < `admin`) plus a `device` role that may only ingest;
add `--site <id>` to scope a user to one site:

```bash
docker compose exec api uv run python -m minemonitor.auth.cli alice admin
docker compose exec api uv run python -m minemonitor.auth.cli dev1 device
docker compose exec api uv run python -m minemonitor.auth.cli bob viewer --site kn-zw-01
```

The CLI prompts for a password (or reads `MM_NEW_USER_PASSWORD`). Trackers/adapters
must authenticate as a `device` user to POST positions.

## 5. View it — safely

The API now has **HTTP Basic auth with roles and per-site scoping** (M6). Basic
credentials are only as safe as the transport, so still do **not** expose plain
port 8000 to the public internet — put TLS in front. Two safe options:

**a) SSH tunnel (simplest, nothing to configure):**
```bash
# from your laptop:
ssh -L 8000:127.0.0.1:8000 <user>@<vps-host>
# then open http://localhost:8000/
```

**b) Public URL behind nginx + TLS:** put nginx in front, terminate Let's Encrypt
TLS, and `proxy_pass` to `127.0.0.1:8000`. The **application** now handles login
(HTTP Basic with roles), so nginx only needs to provide TLS — do not add a second
`auth_basic` layer or the browser will prompt twice. SSE needs buffering off:
```nginx
server {
  server_name mine.example.com;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;   # pass Basic creds through
    proxy_buffering off;                 # required for Server-Sent Events
    proxy_read_timeout 1h;
  }
}
```
Then `certbot --nginx -d mine.example.com`.

## 6. Real data vs. demo

- **Demo:** the `simulator` service publishes a synthetic fleet over MQTT — this is
  what fills the dashboard today (no hardware needed).
- **Real trackers (later):** stop the simulator (`docker compose stop simulator`)
  and point trackers/adapters at the broker; nothing else changes at the ingest
  boundary.

## 7. Operate

```bash
docker compose ps                 # status + per-service health
docker compose logs -f api        # API/dashboard logs
docker compose logs -f ingestor   # ingest + periodic cycle recompute
docker compose down               # stop (keeps data volumes)
```

**Resilience & health:** every long-running service has `restart: unless-stopped`
and a healthcheck, so a crash or reboot brings the stack back up (power will fail
at the site). Two probes:
- `GET /healthz` — liveness (API + database). This is what the api container's
  healthcheck and `deploy/deploy.sh` wait on.
- `GET /health` — full system: also MQTT and the ingestor heartbeat; returns 503
  when either is down, so a stuck ingestor or dead broker is visible to
  monitoring. `docker compose ps` shows each container's own health.

**Auth lockout:** an account locks after `MM_LOGIN_MAX_FAILURES` consecutive
failed logins for `MM_LOGIN_LOCKOUT_MINUTES` (audited). If a real user locks
themselves out, either wait out the window or reset their password with the CLI
(`docker compose exec api uv run python -m minemonitor.auth.cli <user> <role>`),
which clears the lock.

**Scheduled backups:** run the backup sidecar alongside the stack —
```bash
docker compose --profile backup up -d     # dumps ./backups on MM_BACKUP_INTERVAL_S
```
or invoke `deploy/backup.sh` from host cron. Old dumps are pruned after
`MM_BACKUP_KEEP_DAYS`.

**Retention & audit:** the ingestor runs a per-data-class deletion job (~daily;
positions / metrics+cycles / events / audit trail, each configurable in days via
`MM_RETAIN_*`, `0` = keep forever). An admin can trigger it on demand with
`POST /admin/retention/run` and read the audit trail at
`GET /sites/{site_id}/audit`.

**Personal data (brief §4):** all operator PII lives in the `operators` table;
events and cycles reference an operator only by opaque id. Handle a data-subject
request with `GET /sites/{id}/operators/{op}/export` (access) and
`DELETE /sites/{id}/operators/{op}` (erasure — tombstones the PII, keeps history
valid). Every read of a personal record is audited. No biometric data is stored.

**Backup the database:**
```bash
bash deploy/backup.sh                 # writes backups/minemonitor_<timestamp>.sql.gz
```

**Restore onto a clean box** (M6 acceptance — restore from the compose file + a dump):
```bash
cp .env.example .env && $EDITOR .env  # set the same passwords as the source box
bash deploy/restore.sh backups/minemonitor_<timestamp>.sql.gz
```
`restore.sh` brings up `db`, waits for it to be healthy, loads the dump, then starts
the rest of the stack. (Both scripts wrap `pg_dump`/`psql` and honour the
`POSTGRES_*` values in `.env`.)

## Security checklist before any public exposure

- [ ] Changed `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD` in `.env`.
- [ ] Created real admin/user accounts, then blanked `MM_BOOTSTRAP_ADMIN_*`.
- [ ] API reachable only via SSH tunnel **or** nginx with TLS in front (the app
      provides login; TLS protects the Basic credentials in transit).
- [ ] Firewall (ufw) allows only 22/80/443; **not** 8000/5432/1883/9000.
- [ ] Retention days (`MM_RETAIN_*`) set to the mine's agreed policy.
