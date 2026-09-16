# Feature Plan 08 — Laboratory Data Ingestion

**Status: plan for review.** Part of the RAN Mines alignment. Proposal Phase 1 (laboratory).
Feeds gold reconciliation (FP-09) and loss-pathway intelligence (FP-10).

> **Requirements-meeting update (14 Sep 2026):** the instrument is confirmed as an **Agilent
> 2000-series spectrometer** (SpectrAA software) — currently fully manual. This is the client's
> **Phase 4**. Integration target = SpectrAA's result export/watch-folder. See
> `../RAN_MINES_PROPOSAL_ALIGNMENT.md` §0.

## Purpose

Ingest laboratory instrument output **directly** — removing manual retyping — while
preserving the original result immutably, hashing it, keeping corrections append-only with
actor identity, and detecting anomalous values. Auditability is the point.

## Reuse vs new

- **Reuse:** the immutable-raw / reproducible-derived / annotation split the platform
  already enforces (`positions` immutable, annotations separate); `audit_log`; `operators`
  (actor as FK); MinIO (original files); provenance labelling.
- **New:** `laboratory.result.v1` + `laboratory.correction.v1` contracts, an instrument
  ingest adapter framework, and anomaly checks.

## Capabilities (from the proposal)

- **Direct ingestion:** read instrument output (file/serial/DB/LIMS — per instrument);
  no manual retyping.
- **Immutable original:** store the raw result **write-once**; keep the original file in
  MinIO; compute a **hash/fingerprint** so tampering is detectable.
- **Append-only corrections:** a correction never overwrites; it is a new
  `laboratory.correction.v1` referencing the original, with the **actor** (FK to
  `operators`/`users`) and reason. Full history retained.
- **Anomaly detection:** flag values outside configured/expected bounds (deterministic
  first; models only later, on real history). Flag — never silently alter.

## Data / events

- `laboratory.result.v1`: `instrument · sample_ref · ts · assay/measurement + units ·
  original_file_ref (MinIO) · hash · provenance: measured`.
- `laboratory.correction.v1`: `references result_id · corrected_value · actor · reason ·
  ts` (append-only; original preserved).
- Anomalous value → `event.v1` for review; results feed FP-09 reconciliation.

## Phase 0 dependencies (blocking)

- Instrument **output formats & interfaces** (which instruments, what protocol).
- Units, sample identifiers, and how a lab result maps to a process point / time window.
- Who may correct, and the correction reason taxonomy.

## Slices

1. Ingest one instrument → `laboratory.result.v1` (immutable + hash + original in MinIO).
2. Append-only corrections with actor + history.
3. Deterministic anomaly bounds → `event.v1`.

## Safety / provenance

`measured` provenance with instrument + hash; corrections are annotations, never mutations
of the observed record; actor and reason audited. Answers "what was measured, when, by
which instrument, and who changed it" — the audit backbone reconciliation depends on.
