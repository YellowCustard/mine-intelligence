# Feature Plan 07 — Access Control Integration

**Status: plan for review.** Part of the RAN Mines alignment. Proposal Phase 1 (gate
security). External-integration-heavy — several Phase 0 unknowns.

> **Requirements-meeting update (14 Sep 2026):** the client's Phase 1. Ingest **Alhua gate
> face-events** (events only, no templates) + the **iHUA** visitor tag; tie entry to the
> **roster/timesheet** (allow only if on shift; flag early/late); **random-search generator**
> with an audit trail (alert if a selected search is skipped). Alerts pushed to **WhatsApp**.
> See `../RAN_MINES_PROPOSAL_ALIGNMENT.md` §0.

## Purpose

Connect the mine's existing identity/access infrastructure — face/payroll system, gates,
turnstiles, metal detector — to Mine Monitor by ingesting **access events**, and apply
authorisation and search logic. Mine Monitor is the record and intelligence layer; it does
**not** replace the access hardware.

## Reuse vs new

- **Reuse:** `event.v1` (access decisions surface here), `operators` (identity is a FK,
  never a name in a payload — `CLAUDE.md` §4), `shift_definitions` (on/off-shift),
  `audit_log`, `incidents` (escalations), notifications, RBAC.
- **New:** an `access.event.v1` contract + ingest adapter(s), and authorisation/search rules.

## Capabilities (from the proposal)

- **Gate identity & authorisation:** ingest an access decision from the face system; apply
  rules to reject **off-shift**, **suspended**, **non-inducted** personnel (against operator
  status + shift). Record the event and the decision + reason.
- **Metal detector:** ingest detector events; associate with the gate passage.
- **Random search selection:** select passages for search (configurable rate); **escalate
  missed searches** (a selected passage with no recorded search) → `event.v1`/incident.
- **Shift-change throughput:** handle bursts (many passages at shift change) without loss —
  store-and-forward on the ingest path.

## Data / events

`access.event.v1` (provenance-carrying): `source_system · credential_ref (tag/card/face
event id — NOT a template or image) · operator_ref (FK|null) · gate_id · ts · decision
(granted/denied) · reason · search_selected (bool) · search_completed (bool|null)`.

- **No biometric data ever** enters Mine Monitor: the face system stays the identity
  authority on the mine network; we ingest its **decision events** only.
- Authorisation-failure and missed-search events promote to `event.v1` for the control
  room; feed FP-10 loss-pathway correlation.

## Phase 0 dependencies (blocking)

- Face/payroll system **event interface** (how decisions are exposed).
- Gate / turnstile / **metal detector** protocols & interfaces.
- Operator status source (suspended / inducted / roster) and how it maps to `operators`.

## Slices

1. Ingest access events (adapter) → `access.event.v1` stored with provenance. **✅ DELIVERED**
   — `access.event.v1` contract (registered), migration `0020` (`access_events` table), the
   `access/` service, a simulator-first adapter (`ingest/adapters/access_sim.py`) that refuses
   biometric payloads, and `/api/v1/sites/{id}/access/events`.
2. Authorisation rules (off-shift/suspended/non-inducted) → decision + `event.v1` on deny.
   **✅ DELIVERED** — `authorize()` (reads new `operators.suspended`/`inducted` + `resolve_shift`
   for off-shift); a gate *grant* to someone the rules would reject raises a critical
   `access_denied` `event.v1`; admin `access-status` endpoint sets operator status.
3. Metal-detector association + random-search selection + missed-search escalation.
   **✅ DELIVERED** — a deterministic **random-search generator** selects passages at a
   configurable rate (`MM_ACCESS_SEARCH_RATE_PERCENT`) when the source did not; a
   **search-completion** endpoint records the search + the **metal-detector** result
   (migration `0021`, `access_events.metal_detected`), raising a critical `metal_detected`
   `event.v1` on a positive detection; and **missed-search escalation**
   (`detect_missed_searches`, on the maintenance tick) raises a `search_missed` `event.v1`
   when a selected passage is not searched within the grace window (`MM_ACCESS_SEARCH_GRACE_S`).

**Live gate poller — ✅ SCAFFOLDED** (`ingest/adapters/alhua_gate.py`). A thin driver polls the
Alhua gate API each maintenance tick (`MM_ACCESS_GATE_URL`, off by default) and feeds each raw
event to the same `ingest_access_event` — the normaliser/rules do not change. Resilient:
the poll cursor is derived from the latest stored event (no in-memory state, so a restart
resumes), ingest is idempotent (refetch overlap never double-records), a fetch failure retries
next tick, and one malformed/biometric event is skipped without blocking the batch. The vendor
response shape (`AlhuaHttpGateSource._to_raw`) is a **documented best-effort to verify against
the real backdoor-API spec** when credentials arrive; only that mapping needs adjusting.

## Safety / provenance / data protection

Advisory (Mine Monitor records and alerts; the gate hardware enforces). Identity is a FK to
`operators`; no biometric templates or images; per-site export/delete; audit on
personal-data access (`CLAUDE.md` §4).
