# Feature Plan 06 — Vision Person↔Plant Safety

**Status: plan for review.** Part of the RAN Mines alignment. Depends on FP-02; reuses the
existing safety-event architecture and `VISION_GPS_FUSION.md`.

## Purpose

Detect a person too close to moving mobile plant and raise an **advisory** safety alert.
Equipment classes: haul_truck, excavator, loader, dozer, grader, and other mobile plant.

## Reuse vs new

- **Reuse:** `event.v1` safety alarms (the platform already emits `proximity` — it is in
  the enum), the alarm queue + ack, `assets`/`asset_class`, FP-02 detection/tracking,
  calibrated homography for ground distance (`VISION_GPS_FUSION.md` §6), GNSS/vision
  correlation where the plant is a tracked asset.
- **New:** a proximity rule over co-tracked `person` + plant classes on the ground plane.

## Capability

- Detect + track a `person` and a mobile-plant object in the same camera; estimate ground
  distance via homography; when the distance is below a configured threshold **and moving**
  and sustained (temporal), raise `event.v1` (`type: proximity`, severity by policy).
- Where the plant is also a GNSS asset, **correlate** (fusion) to name the asset with a
  labelled association (`confirmed/probable/possible/unknown`) — never a silent claim.
- Distance basis is `estimated` (homography) or image-space heuristic (labelled) when no
  calibration exists — degrade to a lower-confidence alert, never fabricate a metric.

## Data / events

- Observation: `vision.operational_event.v1` (`type: unsafe_proximity`) with evidence
  (both tracks, distance, window, clip ref).
- Alarm: existing `event.v1` (`proximity`) with `asset_id` (if correlated) + distance +
  confidence in `detail`.

## Phase 0 dependencies

Which camera zones overlook mobile-plant activity; homography calibration for those cameras
(FP-01); proximity thresholds per zone/plant class.

## Slices

1. Person + plant co-detection + image-space proximity alert (uncalibrated, labelled).
2. Homography ground distance → `estimated` distance + threshold rule → `event.v1`.
3. GNSS/vision correlation to attach `asset_id` with a labelled association.

## Safety boundary (hard)

**Advisory only.** The alert warns a person; it never brakes a truck, stops, or actuates
plant (`CLAUDE.md` §15, `event.v1.advisory === true`). A requirement to intervene
physically is escalated, not built.
