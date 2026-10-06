# Installing Mine Monitor from a release

A release is one file you copy to the box and one command you run. It installs on a clean Linux
host with Docker, or upgrades an installed one, and it needs no internet: the offline bundle
carries every image, each pinned by digest. Building from a git checkout (`deploy/DEPLOY.md`)
stays the path for development and demos.

In a release bundle this file is `README.md`.

## 1. What the box needs

- Linux on x86_64. 2 GB RAM minimum, 4 GB comfortable. 10 GB free for Docker, plus the data.
- Docker Engine 28.0 or later, with the Compose plugin 2.20 or later. With internet:
  `curl -fsSL https://get.docker.com | sh`. Offline: install Docker's `.deb` or `.rpm` packages
  ([download.docker.com](https://download.docker.com/linux/)) from a USB stick.
- `tar`, `gzip`, `sha256sum` and `curl` (present on Ubuntu and Debian), and root (`sudo`).
- A clock kept by NTP. Trackers timestamp their positions and the platform checks them against
  this clock, so a drifting clock rejects good data. The installer warns when it is not synced.

`sudo ./install.sh` checks all of this first (`sudo mmctl doctor` runs the same checks).

## 2. Get a release

Each tagged release on GitHub (Releases page of the repository) has:

| File | Size | Use |
|---|---|---|
| `mine-monitor-<version>-linux-amd64-offline.tar` | ~1.2 GB | the mine: everything, images included, installs with no internet |
| `mine-monitor-<version>.tar.gz` | ~70 KB | a box with internet: pulls the same pinned images from the registries |
| `SHA256SUMS` | | checksums of both |

The repository is private, so download while signed in to GitHub, or with
`gh release download v<version> -R YellowCustard/mine-intelligence`. The online bundle pulls
the Mine Monitor images from ghcr.io, which needs `sudo docker login ghcr.io` with a GitHub token
that has `read:packages`. The offline bundle needs neither.

Check the download before carrying it to site:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

The installer checks every file inside the bundle again, so a truncated copy off a USB stick
fails before it changes anything.

## 3. Install

```bash
tar xf mine-monitor-0.2.0-linux-amd64-offline.tar
sudo ./mine-monitor-0.2.0/install.sh
```

It asks one question, the address operators will type in the browser (default: this box's IP
address, and `localhost`), then:

1. checks the host and the bundle;
2. writes `/opt/mine-monitor/shared/.env` (root only) from `.env.example`, with `MM_ENV=prod`,
   a generated database password, a generated broker service account (so the broker refuses
   clients with no login), and generated evidence-store keys (that profile stays off);
3. loads the images from the bundle;
4. starts the stack with `deploy/deploy.sh`, with HTTPS (Caddy) and the daily database backup on;
5. creates the first admin and prints its password, which is also kept, readable by root only,
   in `/opt/mine-monitor/shared/initial-admin-password`. Log in, create the real accounts
   (`deploy/DEPLOY.md` §4a), then delete that file;
6. verifies the release: every container healthy, `/health` green, the ingestor running, the
   expected version answering, and the admin able to log in;
7. dumps the database to `shared/backups/` (see §7).

| Option | Effect |
|---|---|
| `--hostname NAME` | address for the dashboard and its certificate; repeat for several |
| `--acme-email EMAIL` | with a public DNS name in `--hostname`: a Let's Encrypt certificate instead of the box's own CA (ports 80 and 443 must be reachable from the internet) |
| `--no-tls` | no HTTPS proxy; reach the dashboard through an SSH tunnel to `127.0.0.1:8000` |
| `--trackers` | start the Teltonika listener on port 5027 (`deploy/COMMISSIONING.md`) |
| `--demo` | start the simulated fleet |
| `--admin-user NAME` | first admin's username (default `admin`) |
| `--restore DUMP` | start from a database dump instead of an empty database (§7) |
| `--no-backup` | skip the database dump at the end (§7) |
| `--prefix DIR` | install somewhere other than `/opt/mine-monitor` |
| `--yes` | ask nothing |

If the install stops part way (a power cut, a full disk), fix the cause and run the same command
again. It resumes and keeps the secrets it already generated, which the database already uses.
The logs of a failed attempt are in `/opt/mine-monitor/shared/logs/`.

## 4. Reach the dashboard

Open `https://<address>/`. The certificate comes from the box's own certificate authority, so
browsers warn until it is trusted. Trust it once on each operator machine:

```bash
sudo mmctl ca-cert > mine-monitor-ca.crt     # then import it as a trusted root CA
```

Only ports 80 and 443 face the network (5027 too with `--trackers`). The database, broker and
API listen on `127.0.0.1` only. Run commissioning check A8 (`deploy/COMMISSIONING.md`) from
another machine to confirm it.

## 5. Change a setting

Settings live in `/opt/mine-monitor/shared/.env`, which upgrades never touch. Edit it, then:

```bash
sudo mmctl deploy
```

It restarts what changed, verifies the release as on install, and dumps the database (§7).

`COMPOSE_PROFILES` in that file lists the optional services that run: `backup`, `tls`,
`trackers`, `sim`, `evidence` (object store, `docs/OBJECT_STORAGE.md`) and `vision`
(`docs/VISION_DEPLOYMENT.md`). Retention, notifications and the rest are described in
`.env.example`.

## 6. Upgrade

Copy the new release's bundle to the box and run its installer:

```bash
tar xf mine-monitor-0.3.0-linux-amd64-offline.tar
sudo ./mine-monitor-0.3.0/install.sh
```

It finds the installation and upgrades it:

1. checks there is room for a database snapshot and the new images, and loads the images;
2. stops the stack. Trackers and the store-and-forward spools hold their data meanwhile and
   send it when the stack is back;
3. copies the stopped database's volume to a snapshot volume;
4. switches `/opt/mine-monitor/current` to the new release and starts it (the API runs the new
   migrations; `deploy.sh` moves the TimescaleDB extension if the database image changed);
5. verifies it as on install. **If verification fails, it rolls back by itself**: it puts the
   snapshot back, restarts the previous release and says where the failed release's logs are;
6. dumps the upgraded database to `shared/backups/`. A dump restores only onto the TimescaleDB
   version it was taken on, so after an upgrade that moved it, this dump is the first one that
   restores onto the new release.

Settings new in the release take their defaults; the installer lists them. The stack is down for
the snapshot copy and the restart, so pick a quiet moment on a large database.

## 7. Roll back, back up, restore

**Roll back** the last upgrade, when the new release runs but misbehaves:

```bash
sudo mmctl rollback
```

The previous release starts again with the database as it was just before the upgrade. Whatever
was recorded after the upgrade is lost from the live database; it is dumped to
`shared/backups/` first. Only the last successful upgrade can be rolled back, until the next
one succeeds: an upgrade that fails, or rolls itself back, leaves the earlier rollback in place.

**Back up.** The `backup` service dumps the database daily to `shared/backups/` and keeps 14
days (`MM_BACKUP_INTERVAL_S`, `MM_BACKUP_KEEP_DAYS`). On top of that, every install, upgrade,
`mmctl deploy` and rollback ends with a dump once the release has verified, because a dump
restores only onto the TimescaleDB version it was taken on. If that dump fails, the command
reports an error although the release is running: fix the cause (often disk space) and run
`sudo mmctl backup`. `--no-backup` skips it. `sudo mmctl backup [file]` takes one at any time.
A dump that does not complete leaves no file, and a dump will not start when the disk could not
take it with 1 GB to spare. The dumps hold personal data and sit on the same disk as the
database: copy them off the box.

**Restore** a dump onto this box, replacing its database:

```bash
sudo mmctl restore /path/to/minemonitor_20261006_020000.sql.gz
```

The dump must come from the TimescaleDB version this release runs (`sudo mmctl status`). The
current database is dumped first.

Rollback and restore both stop, changing nothing, when that first dump of the current database
fails, for example because the database is not running: start it (`sudo mmctl compose up -d db`)
and run the command again. Only when the current database cannot start and you accept losing
what it holds, add `--no-safety-dump`.

**Move to a new box**, or onto a release from a git-checkout deployment: take a dump on the old
box (`sudo mmctl backup`, or `bash deploy/backup.sh` in the checkout), stop the old stack, and on
the new box run `sudo ./install.sh --restore <dump>`. The new box generates new secrets; users,
their passwords and the provisioned devices come from the dump, so devices keep their broker
logins.

## 8. Day to day

| Command | Does |
|---|---|
| `sudo mmctl status` | release, containers, `/health`, and whether a rollback is available |
| `sudo mmctl logs [service]` | follow the logs (`api`, `ingestor`, `mqtt`, `db`, `caddy`, ...) |
| `sudo mmctl deploy` | apply a change to `shared/.env`, verify, and dump the database (`--no-backup` skips the dump) |
| `sudo mmctl backup [file]` | dump the database |
| `sudo mmctl restore <dump>` | replace the database with a dump |
| `sudo mmctl rollback` | undo the last upgrade |
| `sudo mmctl smoke` | the post-deploy smoke test (`deploy/DEPLOY.md` §4b); it writes one position for an asset `SMOKE-TEST` |
| `sudo mmctl ca-cert` | the HTTPS root certificate |
| `sudo mmctl doctor` | check the host against the release's requirements |
| `sudo mmctl compose <args>` | `docker compose` in the running release's directory |

Provisioning devices, zones and shifts is the same as on any deployment:
`deploy/COMMISSIONING.md` and `docs/OPERATIONS.md` (in the bundle under `deploy/` and `docs/`).
Where they say `docker compose exec api ...`, use `sudo mmctl compose exec api ...`.

## 9. Where things are

```
/opt/mine-monitor/
  current -> releases/0.3.0        the running release
  releases/0.3.0/                  its compose file, configs, scripts and docs
  releases/0.2.0/                  the previous release, kept for rollback
  shared/.env                      settings and secrets (0600)
  shared/backups/                  database dumps (0700)
  shared/mosquitto/                broker password and ACL files, generated from the devices table
  shared/var/models/               vision model weights
  shared/logs/                     logs of failed installs, upgrades and rollbacks
/usr/local/bin/mmctl -> /opt/mine-monitor/current/deploy/mmctl
```

Docker holds the data in the volumes of the compose project `minemonitor` (`minemonitor_db_data`
is the database). Upgrades replace only `releases/` and `current`; nothing in `shared/` or the
volumes is replaced except by a rollback or a restore, which ask first.
