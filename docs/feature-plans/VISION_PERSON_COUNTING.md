# Feature Plan 03 — Vision Person Counting & Area Presence

**Status: plan for review.** Part of the RAN Mines alignment. Depends on FP-02 (edge
platform) and FP-01 (camera→zone mapping).

## Purpose

Count people in defined areas and track area presence — **without facial recognition**.
Examples: smelt house, gold room, restricted areas, emergency mustering areas.

## Reuse vs new

- **Reuse:** `zones/` (the counting area is an operational zone), `shift_definitions`
  (permitted schedules), `event.v1` alarm queue, the edge platform (FP-02).
- **New:** a per-zone headcount aggregation over tracked `person` detections, and
  presence-transition rules.

## Capability

- **Headcount:** number of distinct `person` tracks currently inside a zone's image-region,
  as a `vision.operational_event.v1` (provenance `inferred`, confidence from detection +
  track stability). A rolling count, debounced — not a per-frame number.
- **Presence transitions:** `person entered area`, `person left area`, `person remained`
  (dwell ≥ threshold — see FP-04), `person present outside permitted schedule` (zone +
  `shift_definitions`), `person present in restricted area` (→ FP-04).

## Data / events

- No new contract required for counts beyond `vision.operational_event.v1`
  (`type: area_headcount | area_presence`). A count that breaches a rule (e.g. presence
  off-schedule, over-capacity) promotes to `event.v1`.
- Optional derived rollup: headcount per zone per time bucket (reproducible, like
  `asset_metrics`) for trends/mustering.

## Explicit non-goals

- **No identity.** Counts and presence only; `person`, never a name (`CLAUDE.md` §4). This
  is what makes headcount data-protection-safe.
- No mustering *roll-call by name* — headcount vs expected count only (identity association
  is a separate, consented capability if ever required).

## Phase 0 dependencies

Which zones need counting (gold room, smelt house, muster points); camera coverage and
suitability for those zones (FP-01); expected occupancy / capacity per zone; permitted
schedule source.

## Slices

1. Headcount in one configured zone from tracked persons (MVP).
2. Presence transitions (entered/left/remained).
3. Off-schedule presence (zone + shift) → `event.v1`.
4. Per-zone headcount rollup for mustering/trends.

## Safety / provenance

Advisory; counts labelled `inferred` with confidence and evidence (tracks, frames, window);
the dashboard must show the label. Under-count/occlusion is a known limit — report
confidence, never a false-precise number.
