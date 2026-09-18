# RanMines Camera-AI Capability Assessment

**Owner:** Adeel (AI vision specialist). **Status:** preliminary — the field facts are captured
on the **site visit (Tue 22 Sep 2026)**, our first login access. Prepared at John's request.

**The question I was asked:** *what are the RanMines cameras' AI capabilities* — specifically
**storage**, **metadata**, and **inference resolution** — and can they carry the security
use-cases, or do we need new hardware?

## Estate baseline (known)

~**118 Alhua** (Dahua-OEM) cameras on a **closed** system with no public API — but a vendor
**"backdoor" API is on offer** (via Heath, who installed the CCTV), and the **NVR already
generates AI reports** (incl. vehicle tracking) that currently go **unused**. Single **Starlink**
(on-prem only); the **server room is a container beside a kitchen** (relocation flagged).

## Short answer

- **Storage and metadata are almost certainly sufficient to *ingest*** — we take **event
  metadata, never video** (thin Starlink), so mine-side storage is a retention/evidence question,
  not a bandwidth one. Metadata is tiny; we can retain it far longer than footage.
- **The two open risks are (a) the AI-report access path / field format** (backdoor-API creds +
  spec) and **(b) inference resolution at distance and low light**, which decides whether the
  existing cameras carry the security use-cases or we add cameras at choke points + the gold room.
- Both are answerable on the 22nd once we have login. Nothing here changes our platform posture:
  **count/scene-level events only, no biometric templates** (`CLAUDE.md §4`).

## 1. Storage

| What to establish | Why it matters |
|---|---|
| NVR model(s) + total raw capacity; current **retention days** at present bitrate | sizes evidence-clip retention; tells us how far back we can backfill events |
| Per-camera **bitrate + codec** (H.264 vs **H.265** ≈ 2× difference) | drives the retention maths and any re-encode need |
| Whether **AI event metadata** is stored separately from video, and its retention | metadata is small — we keep it long after footage rolls; this is what we ingest |
| Compute/storage headroom for any **edge inference** we run later | ties to the **server-room relocation** and any GPU box; the kitchen container won't host it |

**Our position:** no video crosses to the cloud. Evidence *clips* are referenced, not streamed,
and retained per data class with the deletion job that already runs. Storage is a mine-side
retention decision, not a platform bottleneck.

## 2. Metadata

| What to establish | Why it matters |
|---|---|
| Which **AI event types** the NVR/cameras already emit — line-crossing, area intrusion, loitering, **vehicle**, face | defines which security use-cases are available for free today |
| Per-event **fields**: timestamp, channel, object class, confidence, bbox, rule name, clip ref | this is exactly the shape our NVR adapter maps to `event.v1` |
| **Access path**: ONVIF metadata stream vs **Dahua SDK/HTTP** (the backdoor) vs DB export; auth method | the one true blocker to going live — see the adapter note below |
| **Time sync** — are camera clocks NTP'd? | events must correlate with GNSS + access events; clock skew corrupts reconciliation |

**Our position:** this is precisely what the **NVR-event ingest adapter** (already built,
`ingest/adapters/nvr.py`) consumes — mapping smart events → `event.v1` into the **unified alarm
queue**, **count/scene-level only, no identity** (face templates stay on the appliance). The one
genuine unknown is the vendor's exact **field names**, isolated to a single mapping function
(`_to_raw`); confirming it against a real payload on the 22nd flips the adapter from
simulator-fed to live with no other change.

## 3. Inference resolution

| What to establish | Why it matters |
|---|---|
| Per-camera **main-stream vs sub-stream** resolution / fps / codec, and **which stream the AI runs on** | on-camera IVS usually runs the **sub-stream** — often too low for small/distant targets |
| **Lighting** per zone (dust, night, backlight) | the dominant accuracy driver on a mine; sets realistic per-camera suitability |
| **Coverage + blind spots** at choke points, gold room, gates | decides where existing cameras suffice vs where we add angles/cameras |

**Our position:** these map 1:1 onto the **FP-01 camera-readiness registry** fields
(`resolution`, `fps`, `codec`, `lighting`, `has_usable_ai_stream`, `ai_suitability`,
`blind_spot_notes`, `zone_id`). Camera **sufficiency** for reliable personnel tracking is only
knowable by running that audit on-site. Consistent with `PHASE1_PERSONNEL_TRACKING_ASSESSMENT.md`:
vision alone won't hit the 99.9% bar, so we commit to **count-reconciliation** (vision + tag +
access), not single-camera identity through masks.

## How this feeds the platform we've already built

| On-site finding | Where it lands |
|---|---|
| Per-camera specs, AI-stream usability, blind spots | **FP-01 camera registry** (`/api/v1/sites/{id}/cameras`), the Phase-0 audit tool — already live |
| A **sample raw AI event** per type + the API endpoint/auth | confirms/adjusts the **NVR adapter** `_to_raw`; then `MM_ACCESS_GATE_URL`-style live polling turns it on |
| Gate face-events + metal-detector feed | **access-control (FP-07)**, merged — the live gate poller is scaffolded, pending the same API spec |

## Capture on the 22nd (what the visit must produce)

1. **NVR:** model, total capacity, current retention days, free space; whether the AI-report store
   is queryable/exportable and its retention.
2. **Metadata:** one **sample raw AI event of each type** (JSON/screenshot export); the API
   endpoint + auth method (backdoor creds via Heath); confirm vehicle/AI reports can be enabled
   per-camera; confirm cameras are NTP time-synced.
3. **Resolution:** stream profiles (main/sub res/fps/codec) for a **representative sample** across
   zone types; **test frames** at the gold room / crusher / a gate; note where new cameras or
   angles are needed.
4. **Infra:** confirm the server-room relocation plan and any space/power for edge compute.

## Dependencies / blockers

- **Backdoor-API creds + field spec** (via Heath) and **NDA** — the go-live blockers for the NVR
  and gate feeds.
- **Coverage map** + per-sector capacities (see `QUESTIONS_FOR_DEREK.md`).
- A **tag decision** (RFID recommended) for the reconciliation half of personnel accountability.

## Recommendation

1. **Treat the 22nd as the data-collection pass**, not a decision meeting: fill the FP-01 registry
   for the 118, export one sample AI event per type, and record stream profiles at the high-value
   zones. That single visit unblocks both the NVR adapter and the personnel-tracking plan.
2. **State plainly to John:** storage/metadata are unlikely to block us (we ingest events, not
   video); the real question is **inference resolution at distance/low light**, which we answer
   per-camera from the audit and turn into a *suitable / marginal / unsuitable + "add cameras at
   X"* verdict.
3. **Keep the honesty line:** measured, per-capability targets over a learning month; count
   reconciliation, never single-camera identity; no biometric data into the platform.
