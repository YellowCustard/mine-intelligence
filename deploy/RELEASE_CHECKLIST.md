# Mine Monitor — Release Checklist

Run through this before cutting a release to a mine site. It is deliberately short and
mechanical — a solo operator should be able to work it top to bottom at speed. Deeper
context is in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md); deployment mechanics in
[`DEPLOY.md`](./DEPLOY.md).

Tick every box. A red box is a blocked release, not a note for later.

---

## 1. Code gate (local, before merge)

- [ ] `ruff check` clean.
- [ ] `ruff format --check` clean.
- [ ] `mypy src` clean.
- [ ] `pytest` green (unit zone-geometry + cycle state machine; integration over the
      simulator).
- [ ] No secrets in the diff. `.env.example` documents every new `MM_*` variable.
- [ ] Type hints everywhere; no bare `except`; all timestamps timezone-aware UTC.

## 2. Migrations

- [ ] Any schema change is a new **additive** Alembic migration off the current head
      (single linear head — no branches).
- [ ] New migration carries `site_id` on every operational table.
- [ ] `alembic upgrade head` then a **down/up round-trip** succeeds on a real
      Postgres 16 + TimescaleDB.
- [ ] `positions` remains immutable; new annotation tables never mutate it; derived
      views still recompute from source.

## 3. Contracts

- [ ] Any contract change is **versioned** (`*.v1` untouched, or a new version added),
      published as JSON Schema in `/contracts`, and mirrored in the Pydantic models.
- [ ] `event.v1` payloads still carry `advisory: true`.
- [ ] Changes stay compatible with the vision repo's `event.v1` shape.

## 4. Security & compliance

- [ ] `MM_ENV=prod` guard verified: the API refuses to boot on sample DB credentials
      or a weak bootstrap password.
- [ ] API bound to localhost; public exposure is only via TLS reverse proxy or SSH
      tunnel (Basic auth must ride TLS).
- [ ] Security headers present (CSP, nosniff, frame-options, referrer-policy).
- [ ] Retention job runs on schedule; per-class `MM_RETAIN_*_DAYS` reviewed for the
      site.
- [ ] **No biometric templates** anywhere in the schema. Operator identity is a
      foreign key only; the `operators` table is the single PII home with export +
      tombstone erasure.
- [ ] Audit log records personal-data access and rule/zone changes.
- [ ] `LICENCES.md` updated for any new dependency; Apache-2.0 / MIT / BSD only.
      **No AGPL** (no Ultralytics YOLO).

## 5. Configuration

- [ ] `.env` on the target has real `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD`
      (not the samples).
- [ ] `MM_ENV` set correctly for the target (dev/staging/prod).
- [ ] Site timezone (`MM_DEFAULT_SITE_TZ`) and `MM_DEFAULT_SITE_ID` correct.
- [ ] MQTT: decide `MM_MQTT_REQUIRE_REGISTERED_DEVICE` (off for a fresh/demo install,
      on once devices are provisioned) and that the broker ACL was regenerated.

## 6. CI / build

- [ ] CI green: test suite + docker build + Chromium dashboard check.
- [ ] `docker compose up` builds cleanly from a fresh clone.

## 7. Deploy & verify on target

- [ ] `bash deploy/deploy.sh` (no `--demo` in production).
- [ ] First admin exists (bootstrap env, or `python -m minemonitor.auth.cli`).
- [ ] `curl /healthz` and `curl /health` both green; `/version` reports the expected
      build.
- [ ] **Post-deploy smoke test passes** (`python -m minemonitor.smoke`, exit 0):
      version, health, auth enforcement, login, ingest round-trip, dashboard.
- [ ] Dashboard reachable over the tunnel/proxy; live SSE updates arriving; alarm
      acknowledgement works.

## 8. Backup / restore proof

- [ ] `bash deploy/backup.sh` writes a dump; `gunzip -t` on it passes.
- [ ] Scheduled backup sidecar running (`--profile backup`) with sane
      `MM_BACKUP_INTERVAL_S` / `MM_BACKUP_KEEP_DAYS`.
- [ ] **Dump/restore data integrity verified** — dump a seeded DB, drop it, restore
      into an empty DB, and confirm row counts, the `alembic_version` head, and a
      content checksum of `positions` all match the pre-dump baseline exactly.
- [ ] **Restore rehearsed end-to-end through the real scripts** —
      `bash deploy/restore.sh <dump>` onto a clean box (the actual `db` container,
      not just a bare Postgres) brings the whole stack up, `/health` is green, and
      the smoke test passes 6/6 against the restored data — proving login works
      (the password hash survived the dump) and bootstrap did not duplicate the
      restored admin. This is the M6 acceptance and is the box that exercises the
      **compose/container plumbing** the data-integrity check above does not.

## 9. Sign-off

- [ ] Open questions for the site surfaced, not guessed (fleet list, onboard weighing,
      connectivity, on-prem requirement, real zone polygons — placeholders clearly
      marked as such).
- [ ] Rollback plan: previous image/tag and last-good database dump identified.
- [ ] Release tagged; `docs/` and `.env.example` reflect what actually shipped.

---

*The system is advisory. Nothing in this release may cross the line into actuating
plant.*
