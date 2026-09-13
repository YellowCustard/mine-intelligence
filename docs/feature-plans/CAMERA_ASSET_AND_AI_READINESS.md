# Feature Plan 01 — Camera Estate & AI Readiness

**Status: ✅ DELIVERED (increment 1) — registry + CRUD + AI-readiness assessment +
estate rollup + `/api/v1` (admin-write, viewer-read, audited) + tests. Migration `0019`
round-tripped on real PostgreSQL 16; `stream_url` is stored but never returned. Recommended
first engineering slice (non-blocked).**
Part of the RAN Mines alignment (`docs/RAN_MINES_PROPOSAL_ALIGNMENT.md`). Proposal Phase 0
requires an audit of all **106 cameras**; this is the structured tool that captures it.

## Purpose

Manage the camera estate and determine, per camera, whether it can support AI analytics —
and if so, on which stream, in which zone, with what calibration and health. It is both a
Phase 0 audit instrument and the registry every vision capability (FP-02…06) reads from.

## Reuse vs new

- **Reuse:** the `assets`/`devices` registry pattern, `sites` tenancy, `/api/v1`, RBAC
  (`admin` write, `viewer` read), `audit_log`, the dashboard, retention. Cameras are
  site-scoped reference data like assets.
- **New:** a `cameras` table + domain module `cameras/` + router. No inference, no model,
  no external integration in this slice.

## Data model (one additive migration)

`cameras` (site-scoped):

`id · site_id · name · location_description · make_model · stream_url (secret) ·
stream_type (rtsp/onvif/file) · primary_or_secondary · resolution · fps · codec ·
lighting_conditions (day/night/mixed/poor) · has_usable_ai_stream (bool|unknown) ·
ai_suitability (suitable/marginal/unsuitable/unknown) · blind_spot_notes ·
zone_mapping (→ zones.id, image-zone → operational zone) · homography_calibration (json|null) ·
calibration_status (none/pending/calibrated) · model_deployed (→ model registry, null) ·
model_version · health_state (online/offline/degraded/unknown) · last_seen · notes ·
created_at · updated_at`

Stream URLs/credentials are **secrets** — never logged, reused from the device-credential
handling posture.

## API (`/api/v1`)

- `GET /api/v1/sites/{site}/cameras` (viewer) — list/filter by suitability, health, zone.
- `POST/PATCH .../cameras` (admin, audited) — create/update audit findings.
- `POST .../cameras/{id}/assessment` (admin) — record an AI-readiness assessment.
- `GET .../cameras/{id}/health` — last-seen/health rollup.

## Phase 0 dependencies

This *is* the Phase 0 camera-audit capture. It needs on-site data (stream availability,
codecs, lighting, blind spots) but the software is buildable now with those fields empty
and populated during the walk-down.

## Slices

1. Registry + CRUD + audit + dashboard list (this slice — non-blocked).
2. Health polling (reachability of the secondary stream) → `health_state`, reusing the
   heartbeat/offline pattern.
3. Zone mapping + calibration-status workflow (feeds FP-02/04).

## Open questions (Phase 0)

- Which cameras expose a usable **secondary** AI stream without disrupting recorders?
- Resolution/fps/codec per camera; lighting by time of day; blind spots.
- Which operational zones (gold room, smelt house, gates, pit areas) each camera covers.
- Edge compute available near each camera (CPU/GPU) — sets the deployment profile.

## Safety / provenance

Reference data only; no events, no inference. It records **where** perception can run and
how trustworthy each camera's view is — the basis for every later vision confidence and
provenance claim.
