# Feature Plan 04 — Vision Security Behaviour

**Status: plan for review.** Part of the RAN Mines alignment. Depends on FP-02, FP-03,
`zones/`, and `shift_definitions`.

## Purpose

Detect security-relevant behaviour in fixed camera zones: restricted-area presence, dwell
(lingering), the two-person rule, scheduled-activity windows, and abnormal presence. Each
produces a structured observation and, when a rule judges it abnormal, an `event.v1`.

## Reuse vs new

- **Reuse:** `zones/` (restricted/generic kinds, debounce/hysteresis), `shift_definitions`
  (schedules), `event.v1` (advisory alarms, ack), FP-03 counting, temporal reasoning (FP-02).
- **New:** behaviour rules over tracks + zones + time + schedule. **Rules are data**, per
  zone — mirroring how zone rules already work; adding one must not need a deploy.

## Capabilities

- **Restricted-area presence:** a `person` track inside a `restricted` zone → `event.v1`
  (`type: restricted_area`, severity by zone policy). Reuses the existing zone rule model.
- **Dwell / lingering:** person in a zone beyond a configured expected dwell (e.g. gold
  room expected X min, actual Y min) → observation; abnormal → `event.v1` (`type:
  zone_dwell`, already in the enum).
- **Two-person rule:** required headcount present during an operation in a zone. Produces
  **evidence + confidence**; **must not claim identity** — it asserts "N people present",
  not who. Below-required count during a defined operation → `event.v1`.
- **Scheduled activity:** presence/activity around a zone outside an expected window (e.g.
  a pour). "Abnormal" is **not** decided by vision alone — it combines vision presence with
  the schedule feed (integration).
- **Abnormal presence:** presence off permitted schedule (FP-03) generalised across zones.

## Data / events

- Observations: `vision.operational_event.v1` (`type: dwell | two_person | scheduled_activity`).
- Alarms: existing `event.v1` types (`restricted_area`*, `zone_dwell`, `proximity`); add
  new `type` values additively only where an existing one does not fit.
  (*`restricted_area` added to the `event.v1` enum when this slice lands.)

## Phase 0 / integration dependencies

- Which zones are restricted / require two-person / have scheduled operations.
- Expected dwell thresholds per zone; required headcount per operation.
- **Scheduling / pour data source** (external) for scheduled-activity — a hard dependency;
  do not hard-code "abnormal" from vision alone.

## Slices

1. Restricted-area presence → `event.v1` (reuses zones).
2. Dwell/lingering thresholds → observation + abnormal `event.v1`.
3. Two-person rule (count + evidence + confidence, no identity).
4. Scheduled-activity monitoring (needs schedule feed).

## Safety / provenance

Advisory only. Two-person and scheduled rules carry evidence (tracks, frames, window,
schedule ref) and confidence; identity is never asserted. Abnormality is a **rule** over
observations, retained and recomputable — never a raw model verdict.
