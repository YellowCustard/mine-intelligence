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
```

## 4. Bring it up

```bash
bash deploy/deploy.sh --demo     # core stack + the simulated fleet (demo data)
# or:  bash deploy/deploy.sh     # core stack only (for real trackers later)
```

This builds the image, starts `db`, `mqtt`, `minio`, `api` and `ingestor` (the API
runs the Alembic migrations on start), and — with `--demo` — the `simulator`.

The API (and the dashboard at `/`) listens on **127.0.0.1:8000** on the VPS.

## 5. View it — safely

The API has **no authentication yet** (that is milestone M6), so do **not** expose
port 8000 to the public internet. Two safe options:

**a) SSH tunnel (simplest, nothing to configure):**
```bash
# from your laptop:
ssh -L 8000:127.0.0.1:8000 <user>@<vps-host>
# then open http://localhost:8000/
```

**b) Public URL behind nginx + TLS + basic-auth:** put nginx in front, terminate
Let's Encrypt TLS, add HTTP basic-auth, and `proxy_pass` to `127.0.0.1:8000`.
Note SSE needs buffering off:
```nginx
server {
  server_name mine.example.com;
  auth_basic "Mine Monitor";
  auth_basic_user_file /etc/nginx/.htpasswd;   # created with: htpasswd -c ...
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
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
docker compose ps                 # status
docker compose logs -f api        # API/dashboard logs
docker compose logs -f ingestor   # ingest + periodic cycle recompute
docker compose down               # stop (keeps data volumes)
```

**Backup the database:**
```bash
docker compose exec db pg_dump -U minemonitor minemonitor > backup_$(date +%F).sql
```

**Restore onto a clean box:** `docker compose up -d db`, wait for healthy, then
`cat backup.sql | docker compose exec -T db psql -U minemonitor minemonitor`.

## Security checklist before any public exposure

- [ ] Changed `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD` in `.env`.
- [ ] API reachable only via SSH tunnel **or** nginx with TLS + basic-auth.
- [ ] Firewall (ufw) allows only 22/80/443; **not** 8000/5432/1883/9000.
- [ ] Application auth/roles land in **M6** — treat this as an internal deployment
      until then.
