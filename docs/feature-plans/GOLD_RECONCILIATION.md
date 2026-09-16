# Feature Plan 09 — Gold Reconciliation

**Status: plan for review.** Part of the RAN Mines alignment. Proposal Phase 3, a
first-class product capability. Depends on FP-08 (lab) + weightometer/production feeds
(Phase 0), and reuses the weighbridge domain's discipline.

## Purpose

Continuously reconcile material through the circuit —
**ore → crusher → mill → tanks → carbon → furnace → final output → gate** — computing
expected vs observed quantities, accounting for normal process variation, and surfacing
**unexplained variance** as an exception for investigation. It does **not** conclude theft.

## Reuse vs new

- **Reuse:** `weighbridge/` is the direct precedent (measured tonnage, idempotent tickets,
  net-consistency **flagged not corrected**); `shift_definitions` (time-window/shift
  association); `events` + `incidents` (exceptions → investigation); FP-08 lab data;
  measured-vs-calculated labelling; provenance.
- **New:** `material.measurement.v1` + `reconciliation.exception.v1` contracts, a
  reconciliation engine, and per-stage expected/observed models with tolerance bands.

## Capabilities (from the proposal)

- **Ingest measurements** (not re-enter): weightometer/crusher scale, laboratory (FP-08),
  carbon measurements, production/pour data — each as `material.measurement.v1` with source
  provenance and quality/status.
- **Associate** each measurement with a **time window** and **shift**.
- **Expected vs observed:** compute expected downstream quantity from upstream measurement +
  assay, compare to observed, **account for normal process variation** (tolerance bands
  baselined from 3–6 months of historical spreadsheets — Phase 0).
- **Exceptions:** variance beyond the configured normal range →
  `reconciliation.exception.v1` → `event.v1`/incident, with the contributing measurements
  and the computed expectation as **evidence**.
- **Auditable trail:** every reconciliation is reproducible from stored measurements.

## The conclusion boundary (critical)

The system states: *"material reconciliation variance exceeds configured normal range,"*
and provides evidence and context. It **must not** hard-code or assert a theft conclusion.
The operational conclusion is a **human** decision — the same posture as dispatch
(recommend→human) and weighbridge (flag→human).

## Data / events

- `material.measurement.v1`: `source · stage (ore/crusher/mill/tanks/carbon/furnace/output/
  gate) · ts · quantity + units · assay_ref (→ lab) · quality/status · provenance: measured`.
- `reconciliation.exception.v1`: `window · stages · expected · observed · variance ·
  tolerance · contributing_measurement_ids · provenance: calculated` (expectation is
  **calculated**, never measured).

## Phase 0 dependencies (blocking)

- Weightometer/crusher **scale data access**; carbon & production/pour **data sources**.
- **3–6 months of spreadsheets** to baseline normal variation per stage (without this,
  tolerances are guesses and exceptions are noise).
- The actual process flow and where each measurement is taken.

## Slices

1. Ingest weightometer + production measurements → `material.measurement.v1`.
2. Stage-to-stage expected-vs-observed with baselined tolerance → variance.
3. `reconciliation.exception.v1` → `event.v1`/incident with evidence + audit trail.

## Safety / provenance

Advisory. Measured inputs labelled `measured` with source/quality; expectations `calculated`
with tolerance; variance flagged, **never** a theft verdict; full reproducibility and audit.
