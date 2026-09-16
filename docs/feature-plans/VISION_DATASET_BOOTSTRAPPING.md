# Feature Plan 12 — Vision Dataset Bootstrapping (DINOv3/DINOv2 frozen backbone)

**Status: plan for review.** Dev/offline capability that accelerates the annotation pipeline
in `../MINING_VISION_DATASET_STRATEGY.md §5`. Depends on nothing in production; it does **not**
run on the edge or in the real-time detection path. Licence-gated: uses **DINOv3** (⭐ accepted
by decision, `../VISION_MODEL_LICENSES.md §1a`) with **DINOv2** (Apache-2.0) as the permissive
fallback.

> **Why now:** a pretrained detector does not understand a Zimbabwean gold mine (dust, night,
> identical PPE, rare equipment), and site labels are scarce and expensive. A **frozen
> self-supervised backbone** turns unlabelled site footage into features we can search, cluster
> and rank — so the few labels we can afford are spent on the *most useful* frames. This is
> upstream of, and independent from, the detector MVP (`../VISION_ARCHITECTURE.md §10`).

## Purpose

Use **DINOv3 frozen features** to reach useful mining accuracy with far fewer labels:

```
unlabelled site frames ──► DINOv3 frozen backbone ──► per-frame / per-crop features
                                                        │
        ┌───────────────────────────┬───────────────────┼───────────────────────┐
        ▼                           ▼                   ▼                         ▼
   retrieval                 few-shot proposal    active-learning           anomaly / novelty
 "find more scenes         "these embeddings     "label these N most     "this scene is unlike
  like this rare one"       look like a          novel / uncertain        anything labelled"
                            water_truck"          frames first"            → flag for review
        └───────────────────────────┴───────────────────┴───────────────────────┘
                                     ▼
                     feeds HUMAN REVIEW in the annotation pipeline
                 (auto-proposals are NEVER trusted unreviewed — §5 rule holds)
```

All four uses are **dev/offline** on the training workstation. None ships to the mine, none
runs per-frame in production, and none produces a trusted label — a human reviews every
proposal before it enters a dataset version.

## The identity boundary (critical)

A backbone emits **feature vectors, never identity.** This capability does **no** face
matching and **no** person re-identification. It never associates a feature vector with a named
individual, and it stores no biometric template (`CLAUDE.md §4`). Scene/crop similarity is used
only to *select frames for labelling* and to *flag novelty* — never to identify a person. Any
future person-identity capability is a separate, consented system and is explicitly out of
scope here.

## Reuse vs new

- **Reuse:**
  - The `VisionModel` adapter boundary (`../VISION_ARCHITECTURE.md §5`) — the backbone is a
    `DinoBackboneAdapter` behind the same interface, so nothing downstream learns the vendor.
  - The **model registry** (`../VISION_ARCHITECTURE.md §6`) — register the backbone as a
    `feature-extractor` task with its `weights_ref`, `license`, and version, exactly like a
    detector; every artefact it produces carries `model.id` + `model.version` provenance.
  - The **"flag, don't conclude" + provenance** discipline (weighbridge/fuel/vision precedent):
    an anomaly flag is advisory and evidence-carrying, never a conclusion.
  - The annotation pipeline (`../MINING_VISION_DATASET_STRATEGY.md §5`) and its immutable,
    hashed **dataset versions** — this feature only changes *which frames* get labelled.
- **New (all dev/offline):**
  - A `DinoBackboneAdapter` with a new `.embed(frame|crop) -> FeatureVector` capability
    (`detect`/`segment`/`classify` remain `NotImplemented` for a pure backbone).
  - A small **embedding / retrieval / active-learning dev tool**: compute embeddings over a
    frame corpus, a nearest-neighbour index for retrieval, a novelty/uncertainty ranker for
    active-learning selection, and a novelty threshold for anomaly flags.

## Scoped, not built (respects the §14 STOP)

This plan **scopes** the adapter and dev tool; it does not add code, weights, or a model
dependency. The vision foundation STOP (`../VISION_ARCHITECTURE.md §14`) holds: no
`src/minemonitor/vision/` code exists yet. Build begins only **after the foundation is
reviewed**, behind the licence gate, and — for anything that would ever leave the workstation —
after the ranked MVP sequence (a detector + ByteTrack) is in place. The temporary signed
weight-download URLs are **not** stored or actioned here; weights are managed through the model
registry / MinIO when the build phase begins, and the DINOv3 weights are **never re-hosted
publicly** (per the DINOv3 License).

## Data / provenance

- Embeddings and the retrieval index are **derived, recomputable dev artefacts** — not
  operational records, and not `vision.*.v1` events (nothing here reaches the alarm queue).
- Any novelty/anomaly flag surfaced for review carries its evidence (the frame ids, the
  reference it was compared against, the distance) and provenance `inferred` — advisory only.
- The backbone's `model.id`/`version`/`license` are recorded in the registry so a dataset
  built with its help is traceable to the exact features that shaped its selection.

## Downstream consumers

Better label-efficient models feed the person/security capabilities that the RAN Mines
requirements prioritise:
- **FP-03 Vision Person Counting** — more accurate headcount from fewer site labels.
- **FP-04 Vision Security Behaviour** — rarer behaviours (dwell, two-person) get labelled data
  via retrieval/active-learning.
- **FP-05 Vision / Tag Reconciliation** — a more reliable vision count strengthens the
  count-reconciliation signal that actually meets the client's accuracy expectation.

See `../RAN_MINES_PROPOSAL_ALIGNMENT.md §7` (indexed as FP-12).

## Verification (when built)

- **Licence gate first:** DINOv3's ⭐ acceptance is recorded (`../VISION_MODEL_LICENSES.md §1a`)
  before any weights are used; weights are never re-hosted publicly; satellite (SAT) weights
  verified per checkpoint if ever used.
- **Adapter conformance:** `DinoBackboneAdapter` implements the `VisionModel` interface; a stub
  backbone is the CI fixture — **no weights or GPU in CI** (mirrors `../VISION_ARCHITECTURE.md §13`).
- **No identity:** tests assert the tool never emits or stores any person-identity association.
- **Selection is advisory:** every proposal routes to human review; no auto-label enters a
  dataset version unreviewed; anomaly flags carry evidence and provenance.
- **Measured value:** a labelling-budget experiment shows the active-learning selection reaches
  a target mAP with fewer labels than random sampling on the site validation set — the payoff
  is measured, not asserted.
