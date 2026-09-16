# Feature Plan 10 — Loss-Pathway Intelligence

**Status: plan for review.** Part of the RAN Mines alignment. The capstone capability —
build last, once at least two upstream sources exist.

## Purpose

Combine events across **camera, access, laboratory, weightometer, production, telemetry and
operational systems** to identify **correlated discrepancies** that warrant investigation —
making loss, security problems and abnormal behaviour visible quickly. This is the
proposal's central business objective realised as a cross-source correlation layer.

## Reuse vs new

- **Reuse:** the unified `event.v1` queue (every source already lands here), the contract
  registry + event bus (`platform/bus.py`) for subscription, `incidents` (investigations),
  `shift_definitions` (time windows), `audit_log`, SSE dashboard.
- **New:** a correlation engine that subscribes to the relevant `*.v1` streams and produces
  correlated findings — **not** a new event framework.

## Capability

- **Correlate across sources over a time/zone window.** Examples the proposal implies:
  - gold-room vision headcount vs tag count vs access-gate count disagree in the same window
    (FP-03 + FP-05 + FP-07);
  - a reconciliation variance at a stage (FP-09) coincident with an access anomaly or an
    off-schedule gold-room presence (FP-04/07);
  - a lab correction (FP-08) that moves a reconciliation from in-range to out-of-range.
- **Output a correlated finding** (reuse `reconciliation.exception.v1` or a
  `loss_pathway`-typed `event.v1`) that links the contributing events as **evidence**, with
  each source's provenance intact.
- **Rank/surface** findings for investigation; a human investigates and decides.

## The conclusion boundary (critical)

It correlates and surfaces; it **does not** conclude wrongdoing or name individuals. A
finding says *"these measurements/events across sources are inconsistent in this window —
investigate,"* with the evidence and each provenance. Consistent with FP-05/FP-09 and
`CLAUDE.md` §4/§15.

## Data / events

- Consumes: `vision.operational_event.v1`, `access.event.v1`, `laboratory.result/correction
  .v1`, `material.measurement.v1`, `reconciliation.exception.v1`, `event.v1`, telemetry.
- Produces: a correlated finding referencing all contributing event ids + windows + each
  provenance; promotes to `event.v1`/incident for the control room.

## Dependencies

- **≥ 2 upstream sources live** (e.g. FP-07 access + FP-09 reconciliation, or FP-03 vision +
  FP-05 tags). Do not build the correlation engine before it has real streams to correlate.
- Consistent **time-window and zone** semantics across sources (shared from `zones` +
  `shift_definitions`).

## Slices

1. Subscribe to two live sources on the bus; correlate on a shared window/zone.
2. Correlated-finding output with linked evidence + provenance → `event.v1`/incident.
3. Add sources incrementally; add ranking/surfacing in the dashboard.

## Safety / provenance

Advisory; correlates, never concludes; every finding is reproducible from the source events
it links, each retaining "what did we know, when, from where, what was inferred, what a
human decided."
