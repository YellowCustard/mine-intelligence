# Mine Monitor — GPS / Vision Fusion

**Status: architecture for review. No fusion code exists.**

Covers Phase 12 (GPS + Vision fusion). This is one of Mine Monitor's most valuable
capabilities and one of its most dangerous if done carelessly: it links what a camera
*sees* to a known GNSS asset. The governing rule, from `PLATFORM_EVOLUTION_ARCHITECTURE.md`
§2.1: fusion is a **correlation** module in the core — **never a silent identity claim.**

## 1. What fusion does (and does not)

- **Does:** decide how likely a camera track and a GNSS asset are **the same physical
  machine**, and label that likelihood — `confirmed | probable | possible | unknown` —
  with the evidence behind it.
- **Does not:** rename a track to an asset, overwrite either source, or assert identity
  without a confidence label. A camera track stays a camera track; an association is an
  annotation *linking* the two, always qualified.

```
 GNSS:   asset HT-014 · lat/lon · speed · heading · ts        (asset.position.v1 — measured)
 Vision: track cam-04:37 · haul_truck · ground_point · ts     (vision.observation.v1 — observed)
                                   │
                          Fusion correlation
                                   │
        association ∈ {confirmed, probable, possible, unknown} + confidence + evidence
                                   │
        → subject.asset_id + subject.association on vision.operational_event.v1
```

## 2. The correlation signals

Fusion scores a (track, asset) pair over a short time window using signals that are
cheap, explainable, and each independently defeasible:

| Signal | How | Weight of evidence |
|---|---|---|
| **Spatial proximity** | distance between the track's `ground_point` (homography) and the asset's GNSS position, both in WGS84 | primary — but only if the camera is calibrated |
| **Time** | observations and positions aligned to the same instant (± tolerance for clock skew/backfill) | gating — mismatched time → reject |
| **Camera zone** | is the asset's GNSS position inside this camera's field-of-view footprint? | gating — asset not in view → not this track |
| **Class consistency** | detected `class` vs the asset's `asset_class` (`haul_truck` track ↔ `haul_truck` asset) | strong — class mismatch → reject |
| **Motion agreement** | track image-motion / ground-speed vs GNSS speed & heading | corroborating |
| **Uniqueness** | is there exactly one plausible asset, or several equidistant candidates? | ambiguity → cap at `possible` |
| **Visual characteristics** (later) | fleet number/colour where legible; a permissive ReID cue | corroborating, optional |

The camera's **field-of-view footprint** (which site zones/ground area it can see) and
its **ground-plane homography** are configured per camera (`VISION_DEPLOYMENT.md` §4) —
without calibration, spatial signals are absent and associations cap low.

## 3. The association ladder (never overclaim)

```
 confirmed   one asset, in view, class-consistent, spatially + temporally tight,
             no competing candidate                          (high confidence)
 probable    strong spatial/time/class agreement, minor ambiguity or looser calibration
 possible    plausible but ambiguous — multiple candidates, weak calibration, or partial signals
 unknown     insufficient basis — no calibration, no GNSS in window, class mismatch, or conflict
```

- The result is written to `vision.operational_event.v1`'s `subject.association` with
  `provenance: correlated` and its confidence + evidence (which signals fired, the
  candidate set, the time window).
- **A wrong association is rejected, not downgraded silently.** Class mismatch, asset not
  in the camera footprint, or a time gap beyond tolerance forces `unknown` — the module
  refuses to guess. This is a tested behaviour (`VISION_ARCHITECTURE.md` §13: "a wrong
  association is rejected").
- Ambiguity (several equidistant candidate assets) is capped at `possible` and never
  promoted to a single asset id.

## 4. Where fusion adds value

- **Cross-validation of operational state.** Vision "truck queueing at load face" +
  GNSS "HT-014 stationary in the load zone" agreeing → high-confidence operational
  event. Disagreeing → a **data-quality signal**, not a silent overwrite (mirrors how the
  platform treats clock-skew and telemetry-integrity today).
- **Filling each other's gaps.** GNSS says *where* a known asset is even when it is out
  of frame; vision sees machines with **no tracker** (a contractor light vehicle, a
  visitor) that GNSS cannot. An unassociated but detected `light_vehicle` in a
  `restricted` zone is exactly the kind of alarm vision uniquely enables.
- **Better haul-cycle truth.** Vision loading/dumping interactions corroborate the
  GNSS-derived haul cycle and its queue-time metric — the commercial centre of gravity —
  without new hardware on the truck.
- **Hard examples for training.** GNSS/vision disagreements feed the active-learning
  queue (`VISION_TRAINING.md` §5).

## 5. Guarantees & invariants

- **No silent identity.** Every association is labelled and evidenced; the dashboard
  renders the label (`confirmed/probable/possible/unknown`), never a bare asset name from
  a camera.
- **Neither source is overwritten.** GNSS positions and vision observations remain
  independent, immutable records; fusion produces a *link*, stored in the derived layer,
  recomputable when calibration or rules improve.
- **`asset_id` is always a foreign key** to `assets` (`CLAUDE.md` §4) — fusion selects an
  existing asset or none; it never invents one, and it never stores a person's identity.
- **Advisory only.** A fusion-driven alarm is still `event.v1` with `advisory: true`; it
  informs a person and never actuates plant.
- **Degrades cleanly.** No calibration → no ground distance → associations cap at
  `possible/unknown` and proximity alarms fall back to image-space heuristics clearly
  labelled `estimated`; the system says "unknown", it does not fabricate certainty.

## 6. Relationship to depth/spatial reasoning

Fusion's spatial signal relies on the **calibrated homography** ground point, not learned
monocular depth (`VISION_MODEL_CATALOG.md` §5) — homography is more accurate and
explainable for a fixed camera on a ground plane, and a proximity/association decision
must be explainable. Learned depth, if ever added, is a *relative* corroborator only,
never the basis of a `confirmed` association or a safety alarm.

**STOP:** fusion is specified as a correlation module with a labelled association ladder
and mandatory evidence. It is built as an in-core module during the vision phase, after
these documents are reviewed — never as a silent identity resolver.
