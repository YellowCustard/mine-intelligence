# Phase 1 — Personnel Tracking Feasibility Assessment

**Owner:** Adeel (AI vision specialist). **Status:** preliminary, pending Alhua API access +
NDA + a coverage map. Prepared following the requirements meeting.

**The question I was asked to answer:** *can the existing **118 Alhua cameras** (via the
vendor "backdoor" API / NVR AI reports) do reliable per-zone personnel tracking across the
mine, or do we need new hardware?*

## Short answer

- **Vision alone will not meet the client's 99.9% bar.** Cross-zone personnel *identity*
  tracking on a mine — masks on the floor, identical PPE, dust, rain, and non-overlapping
  cameras — is the hardest case in the field. Masks defeat face identity; hand-off between
  cameras that don't overlap is where trackers lose people.
- **The reliable, committable approach is vision + a physical tag**, reconciled: cameras give
  a **count and presence per zone**; a **tag** (RFID wristband or tear-off strap) gives an
  identified count; the platform **reconciles the two** and alarms on the discrepancy. This is
  exactly the "6 people in a 5-person zone = breach" idea from the meeting, made reliable.
- **Camera sufficiency is unknown until we audit the 118** against a coverage map. Expect: good
  enough for **counting/presence in fixed rooms** (gold room, elution, muster), **gaps** on the
  paths between zones, and a need for **a few new cameras** at choke points + edge compute
  (which also needs the server room moved out of the kitchen container).

## Why 99.9% single-modality vision is the wrong promise (and what to promise instead)

The theft concern is workers coating hands in high-concentrate **gold slurry (dust, not
nuggets)** post-crusher/post-cyanide. The client's stated preference is **prevention** —
restrict and account for who is where — over detection. That plays to our strengths **if** we
stop trying to identify individuals from a camera and instead **reconcile counts**:

| Capability | Vision-only realistic accuracy | With vision + tag reconciliation |
|---|---|---|
| "How many people are in this zone?" (headcount) | good in a fixed, well-lit room; degrades with occlusion/dust | **high** — two independent counts cross-checked |
| "Is the zone over its allowed capacity?" | good | **high** |
| "Is an *un-tagged* person present?" (tag count > vision, or vision > tag) | n/a | **high** — this is the real theft signal |
| "*Which named person* is in the gold room at 2am?" | **poor** (masks) — do not promise | only via the tag's identity, never the camera |

So we reframe acceptance (see `QUESTIONS_FOR_DEREK.md` §G): **measured, per-capability targets
over a one-month learning period**, not a single 99.9% number. Count-reconciliation can be made
very reliable; single-camera identity through masks cannot, and we will not claim it.

## The approach, mapped to what we already have

Prevention-first personnel accountability, built on the existing Mine Monitor spine:
- **Zones** (`zones/`) already do polygons + debounce/hysteresis → the mine's sectors.
- **Headcount per zone** = `VISION_PERSON_COUNTING.md` (FP-03): count `person` tracks per zone.
- **Zone-occupancy breach** ("N in an (N-1) zone") = a new per-zone `max_occupancy` rule on the
  existing rules engine → `event.v1` (advisory, into the same alarm queue). *This is the
  recommended first code slice and needs no camera access to build/test.*
- **Tag reconciliation** = `VISION_TAG_RECONCILIATION.md` (FP-05): camera count vs tag count →
  discrepancy alarm. **Never names a person** without an independently validated identity.
- **Restricted-area / dwell / behaviour** = `VISION_SECURITY_BEHAVIOUR.md` (FP-04).
- **Entry rules** (allow only if on shift; flag early/late; random search) =
  `ACCESS_CONTROL_INTEGRATION.md` (FP-07), fed by the Alhua gate face-events (events only) +
  the timesheet/roster.
- **Alerts to WhatsApp** = the notifications outbox with a WhatsApp channel added.

Tag technology note (from the meeting): **BLE has a helmet-swap tamper risk**; **RFID
wristbands or tear-off straps** are harder to swap and are the safer default. Final choice is
the client's; the reconciliation logic is tag-agnostic.

## Camera sufficiency — what the audit must establish

Run the **camera AI-readiness audit** (`CAMERA_ASSET_AND_AI_READINESS.md`, FP-01 — the registry
is already built) against all 118, capturing per camera: usable AI stream (via the Alhua API or
sub-stream), resolution/fps/codec, lighting, **coverage of the target zone**, **overlap with
neighbours** (for hand-off), blind spots, and mounting. Likely findings:
- Fixed high-value rooms (gold room, elution) — usable for counting/presence.
- Paths between zones — **coverage gaps**; either accept count-at-zone-only (no continuous
  track) or add cameras at choke points.
- Distant/plant cameras — marginal (dust, backlight, distance).
- **Edge compute** must live in a real server room, not the kitchen container.

## Dependencies / what unblocks this
- **Alhua API access + NDA** (blockers).
- **Coverage map** + per-sector capacities from Derek (`QUESTIONS_FOR_DEREK.md` §B, §C).
- A **tag decision** (RFID recommended).

## Recommendation
1. **Now (non-blocked):** build the **zone-occupancy / headcount-breach** rule on the existing
   zones+events spine and a **WhatsApp** alert channel — demonstrable on simulated or any
   position source, with vision/tag plugging in later.
2. **On API access:** run the FP-01 audit on the 118; return a per-camera suitable/marginal/
   unsuitable verdict and a "new cameras needed at X" list.
3. **Commit to counts, not identities:** promise measured count-reconciliation accuracy over a
   learning month; pair vision with an RFID tag; never promise single-camera identity through
   masks.
