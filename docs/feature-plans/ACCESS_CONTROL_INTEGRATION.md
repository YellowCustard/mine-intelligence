# Feature Plan 07 — Access Control Integration

**Status: plan for review.** Part of the RAN Mines alignment. Proposal Phase 1 (gate
security). External-integration-heavy — several Phase 0 unknowns.

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

1. Ingest access events (adapter) → `access.event.v1` stored with provenance.
2. Authorisation rules (off-shift/suspended/non-inducted) → decision + `event.v1` on deny.
3. Metal-detector association + random-search selection + missed-search escalation.

## Safety / provenance / data protection

Advisory (Mine Monitor records and alerts; the gate hardware enforces). Identity is a FK to
`operators`; no biometric templates or images; per-site export/delete; audit on
personal-data access (`CLAUDE.md` §4).
