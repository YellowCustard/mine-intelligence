# Feature Plan 02 — Mine Monitor Vision Edge Platform

**Status: plan for review.** Part of the RAN Mines alignment
(`docs/RAN_MINES_PROPOSAL_ALIGNMENT.md`). Detailed architecture in `VISION_ARCHITECTURE.md`,
`VISION_DEPLOYMENT.md`, `VISION_EVENT_CONTRACTS.md`; this plan operationalises them for
RAN Mines and defines the edge runtime as buildable increments.

## Purpose

The edge runtime that turns a camera's secondary stream into structured
`vision.observation.v1` / `vision.operational_event.v1`, near the camera, without sending
raw video to the core.

```
camera secondary stream → decode → frame sample → detect → track
  → spatial reasoning (image-zone, homography) → temporal reasoning
  → structured observation → MQTT mm/<site>/vision/<cam> → core ingest → event.v1
```

## Reuse vs new

- **Reuse:** the event contract discipline; MQTT + broker `device`-role credentials + ACL
  (`mm/<site>/vision/<cam>`); store-and-forward spool pattern; MinIO for evidence clips;
  the contract registry (`platform/contracts.py`) for validation at the core boundary; the
  camera registry (FP-01) for stream/zone/calibration.
- **New:** a separate edge **process/service** (not in the core), a **model adapter**
  interface, tracking layer, spatial/temporal engines, and the two `vision.*.v1` contracts.

## Scope (from the proposal + vision docs)

- **Stream ingestion:** RTSP/ONVIF secondary stream (or recorded file for dev); reconnect
  with backoff; drop-oldest under backpressure.
- **Frame processing:** adaptive sampling (2–5 fps default); no per-frame operational
  decisions (temporal reasoning required).
- **Model adapter:** `VisionModel.detect/segment/classify` (vendor-isolated); tracking is a
  **separate** layer (ByteTrack/OC-SORT). Permissive models only (`VISION_MODEL_CATALOG`).
- **Spatial reasoning:** image-zone test mapped to operational `zones`; calibrated
  homography → ground point (`estimated`).
- **Temporal reasoning:** per-track windowed state, debounce/hysteresis (the GNSS-geofence
  discipline applied to vision).
- **Evidence:** short local clip on a promoted event; only a **reference** crosses.
- **Health:** per-camera fps/latency/dropped-frames → camera `health_state` (FP-01).
- **Config:** per-camera zones, thresholds, model version — data, admin-managed, audited.
- **Offline behaviour:** buffer observations locally, backfill on reconnect (M2 no-loss
  bar); core unaffected if the edge node is down.
- **Resource management:** per-camera fps allocation within CPU/GPU headroom; CPU-small
  default (no assumed GPU).

## Contracts

`vision.observation.v1` (raw, immutable, provenance `observed`) and
`vision.operational_event.v1` (interpreted, `inferred/correlated/estimated`, evidence) —
full schemas in `VISION_EVENT_CONTRACTS.md`. Register in `platform/contracts.py`; validate
at the core boundary; operational events that warrant a human promote to **existing**
`event.v1` (`source: "vision:<cam>"`).

## Phase 0 dependencies

Camera secondary-stream availability, codecs, and edge compute per location (FP-01 / site
audit). No live camera needed for slice 2 dev — a recorded clip suffices.

## Slices

1. Recorded file → permissive detector → ByteTrack → `vision.observation.v1` stored (MVP).
2. Image-zone mapping + one temporal rule → `vision.operational_event.v1` → `event.v1`.
3. Live secondary-stream ingestion + reconnect/backpressure + health.
4. Offline buffer + backfill; evidence-clip reference to MinIO.

## Open questions

Edge hardware per site; secondary-stream codec support; acceptable fps per camera; clip
retention policy per data class.

## Safety / provenance

Advisory only; raw video stays on the edge; every observation carries model id/version +
provenance; the core is model-agnostic (it never learns which model produced an
observation).
