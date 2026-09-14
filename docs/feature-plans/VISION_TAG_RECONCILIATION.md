# Feature Plan 05 — Vision / Tag Headcount Reconciliation

**Status: plan for review.** Part of the RAN Mines alignment. Depends on FP-03 (vision
headcount) and an external tag system (Phase 0 unknown).

> **Requirements-meeting update (14 Sep 2026):** the site's tags are the **iHUA** visitor tag
> and (recommended for staff) an **RFID wristband / tear-off strap** — **BLE has a helmet-swap
> tamper risk**. This reconciliation is how the client's **99.9%** expectation is actually met:
> two independent counts cross-checked, never a named individual from a camera. See
> `../RAN_MINES_PROPOSAL_ALIGNMENT.md` §0.

## Purpose

Reconcile a camera-derived headcount for an area against the count of active cap-lamp tags
in that area, and surface the **discrepancy** for investigation.

```
Vision:  "there are 7 people in the gold room"   (FP-03, inferred + confidence)
Tags:    "there are 8 active tags in the gold room"  (external tag system)
         → discrepancy: 1 (vision < tags)  → reconciliation observation → possible event
```

## The identity boundary (critical)

The initial and only committed capability is **count reconciliation**. The system asserts
"vision count 7 vs tag count 8, discrepancy 1" — it must **not** assert "person X has no
tag" or name any individual, unless an independently validated identity association exists
(which this feature does not create). This keeps it within `CLAUDE.md` §4 (no biometric
identity in the platform).

## Reuse vs new

- **Reuse:** FP-03 headcount; `event.v1`; `zones` (the reconciliation area); the
  "flag variance, never conclude" discipline (weighbridge/fuel precedent); provenance.
- **New:** ingestion of tag-system per-area active counts, time-aligned to the vision
  count; a reconciliation rule with a configurable tolerance/latency window.

## Data / events

- Ingest tag counts as timestamped per-area measurements (a small adapter; shape TBD by the
  tag system). Consider `material.measurement.v1`-style provenance or a dedicated
  `access`/count contract — **decide when the interface is known**, not now.
- Reconciliation output: `reconciliation.exception.v1` (shared with FP-09/FP-10) or a
  `vision.operational_event.v1` variant — a discrepancy beyond tolerance → `event.v1` for a
  human. Both counts, the window, and both provenances travel with it.

## Phase 0 / integration dependencies (blocking)

- **Tag system protocol** and how per-area active counts are exposed (reader API / feed).
- Area ↔ camera-zone ↔ tag-zone mapping alignment.
- Expected latency/skew between a person appearing on camera and their tag registering →
  the reconciliation window and tolerance.

## Slices

1. Ingest tag per-area counts (adapter) with provenance.
2. Time-align vision (FP-03) and tag counts; compute discrepancy.
3. Tolerance rule → `event.v1` on sustained/material discrepancy, with both counts as
   evidence.

## Safety / provenance

Advisory. Both source counts labelled (`inferred` for vision, source/quality for tags);
never an identity claim; a discrepancy is a prompt to investigate, not a conclusion.
