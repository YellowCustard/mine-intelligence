# Evidence object storage — SeaweedFS evaluation (2026-10-05)

**Decision:** the evidence object store is **SeaweedFS 4.48** (`chrislusf/seaweedfs:4.48`,
Apache-2.0), run as an **opt-in** compose profile:

```bash
docker compose --profile evidence up -d
```

A plain `docker compose up` no longer starts an object store. Nothing in Phase 1 writes evidence;
the only S3 client in the codebase is the vision edge node with `MM_EDGE_EVIDENCE=s3`
(`vision/edge/s3.py`). The core API and ingestor never touch S3.

## Why MinIO went

| | MinIO (before) | SeaweedFS (now) |
|---|---|---|
| Image | `minio/minio:RELEASE.2024-10-13T13-34-11Z`, **404 on Docker Hub**; upstream community repo archived | `chrislusf/seaweedfs:4.48`, published 2026-09-28 (amd64, arm64, arm/v7, 386) |
| Licence | AGPL-3.0 | Apache-2.0 (open-source edition; the separate Enterprise edition is not used) |
| Known issues at our pin | CVE-2025-31489, CVE-2025-62506; default credentials `minioadmin` | none open at 4.48. The S3 gateway had four access-control CVEs in 2026, all fixed by 4.34: [CVE-2026-54917](https://vulert.com/vuln-db/CVE-2026-54917) (fixed 4.30), [CVE-2026-55873](https://www.sentinelone.com/vulnerability-database/cve-2026-55873/), [CVE-2026-55874](https://vulert.com/vuln-db/CVE-2026-55874), [CVE-2026-58372](https://www.sentinelone.com/vulnerability-database/cve-2026-58372/) (fixed 4.34). Keep the exact pin and bump deliberately; the daily research run watches it |

## What was verified, and how

The container image could not be run in the build environment (no Docker daemon). The **release
binary of the same version** (`linux_amd64.tar.gz` from the 4.48 GitHub release, MD5 checked
against the published `.md5`) was run with the exact flags from `docker-compose.yml`, started
through `docker/objectstore-start.sh`.

| Check | Result |
|---|---|
| Edge `S3Uploader.put` (stdlib SigV4, path-style) of a binary still to `<site>/vision/<cam>/<date>/…`, key with a space and `+` | 200; admin read-back byte-identical |
| Wrong secret | 403 |
| Edge identity: read, list, create bucket, write to another bucket | 403 for each |
| Edge identity: delete an object in the evidence bucket | **204 (allowed)** — see residual risks |
| Anonymous GET | 403 |
| Bucket bootstrap (`objectstore-init` command as rendered by `docker compose config`) | 200, then 409 `BucketAlreadyOwnedByYou` on re-run (exit 0); wrong admin secret gives 403 (exit 1) |
| S3 lifecycle rule (`PUT ?lifecycle`, 30-day expiry on a prefix) | accepted and read back; actual expiry not observed (needs days) |
| Listening sockets | S3 HTTP 8333 on all interfaces; master, volume and filer on 127.0.0.1 only; Iceberg (8181) and Lance (9101) catalog ports off |
| Usage telemetry | off (`-master.telemetry=false`); upstream default is **on**, posting to `telemetry.seaweedfs.com` |

## Hardening in the compose service

- **Fail closed.** `docker/objectstore-start.sh` refuses to start unless the admin and edge
  credentials are set (keys ≥ 3, secrets ≥ 16 characters, restricted character set because they
  are written into JSON), the two keys differ, and the bucket name is valid. There is no
  anonymous identity and no default password.
- **Least privilege.** Two identities: `admin` (operators) and `edge` (`Write:<bucket>` only).
- **Only the S3 port is reachable.** SeaweedFS's master, volume and filer HTTP/gRPC APIs are
  unauthenticated unless JWT signing and gRPC mTLS are configured, so they bind to loopback inside
  the container (`-ip.bind=127.0.0.1`, `-s3.ip.bind=0.0.0.0`).
- **Own network.** The store sits on the `evidence` network, shared only with `objectstore-init`
  and `vision-edge`; the API, ingestor, broker and tracker listener cannot reach it.
- **Host exposure.** S3 is published on `${MM_OBJECTSTORE_BIND:-127.0.0.1}:8333`. Set the LAN
  address only when an edge box elsewhere uploads, behind TLS if that link leaves the site LAN.

## Residual risks

1. **The edge identity can delete or overwrite** evidence in its bucket (SeaweedFS's `Write`
   action includes delete). A stolen edge credential could erase stills. If evidence becomes
   legally significant, add bucket versioning or object lock before relying on it.
2. **S3 gRPC port (8333 + 10000) binds with the S3 port.** It carries SeaweedFS's IAM-cache RPCs
   (`PutIdentity`, `PutPolicy`, …), inferred from the binary's exported method names and not
   exercised here. It is not published to the host and is reachable only from the `evidence`
   network, i.e. from `vision-edge`. Configuring gRPC mTLS (`security.toml`) would close it.
3. **Not backed up.** The `backup` profile dumps the database only. Evidence stills cannot be
   re-created, so losing the `objectstore_data` volume loses them; decide retention and backup
   with the mine before `s3` mode goes live.
4. **Retention is not automated.** Set an S3 lifecycle rule on the bucket for the agreed
   evidence retention (`docs/VISION_DEPLOYMENT.md` §8).

## Not evaluated

Other permissively licensed S3 servers were not tested. Garage was ruled out on licence
(AGPL-3.0).
