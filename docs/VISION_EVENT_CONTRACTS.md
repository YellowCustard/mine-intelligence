# Mine Monitor — Vision Event Contracts

**Status: contract design for review. These schemas are specified, not yet implemented
as JSON Schema files or Pydantic models, and nothing is registered on the running
platform.** On approval they land in `contracts/` + `src/minemonitor/contracts/` and
register in `platform/contracts.py`, exactly like the existing `*.v1` contracts.

Covers Phase 11 (standard vision output). Two new contracts, plus reuse of the existing
`event.v1`. The design rule: **fit the existing event architecture, don't invent a
parallel one.** `event.v1` already reserves the vision seams (`source: "vision:cam-01"`,
`type: "proximity"`, `evidence.clip_uri`) — vision alarms *are* `event.v1`.

## The three-layer contract model

```
 vision.observation.v1        raw perception, immutable   (edge → core, high volume)
        │  temporal + spatial + fusion rules (core / edge)
        ▼
 vision.operational_event.v1  interpreted, evidenced      (derived, reproducible)
        │  when it warrants a human
        ▼
 event.v1                     the unified alarm queue      (EXISTING — unchanged)
```

- **`vision.observation.v1`** — what the model saw in a frame. Authoritative, write-once,
  never edited (like `positions`). This is what operational events are recomputed from.
- **`vision.operational_event.v1`** — a mining-operational conclusion (`queueing`,
  `loading_complete`, `unsafe_proximity`) derived by rules over observations + tracking +
  zones + time + GNSS. Reproducible; carries provenance + evidence.
- **`event.v1`** — unchanged. An operational event that warrants a human becomes a plain
  `event.v1` with `source: "vision:<cam>"`, landing in the same alarm queue as GNSS and
  gate events. The control room groups by severity, not source.

Every layer carries **provenance** and, where not directly observed, **evidence** and
**confidence** — the platform-wide honesty discipline (`observed | inferred | correlated
| estimated | unknown`).

---

## 1. `vision.observation.v1` (raw perception)

```json
{
  "schema": "vision.observation.v1",
  "site_id": "kn-zw-01",
  "camera_id": "cam-rom-01",
  "ts": "2026-09-05T11:42:07Z",
  "received_at": "2026-09-05T11:42:09Z",
  "frame_id": "01J9Z8...",
  "model": { "id": "mine-equipment-detector", "version": "0.1.0" },
  "image": { "width": 1920, "height": 1080 },
  "objects": [
    {
      "track_id": "cam-rom-01:37",
      "class": "haul_truck",
      "confidence": 0.94,
      "bbox": { "x1": 0.31, "y1": 0.44, "x2": 0.52, "y2": 0.71 },
      "ground_point": { "lat": -17.8253, "lon": 31.0336, "basis": "homography" },
      "attributes": { "ppe": null },
      "provenance": "observed"
    }
  ],
  "advisory": true
}
```

Field notes:
- `ts` = frame capture time (device/edge clock); `received_at` = core arrival — the gap
  detects buffered backfill, exactly as `asset.position.v1` does.
- `bbox` is **normalised** [0,1] (image-size-independent; the adapter normalises every
  model to this).
- `track_id` is namespaced by camera (`<camera_id>:<n>`) — it is temporal identity
  *within one camera*, **never an `asset_id`**. Fusion (`VISION_GPS_FUSION.md`) is what
  may associate it with an asset, and only with a confidence label.
- `ground_point` is optional; present only when a calibrated homography exists, with
  `basis` (`homography`) so it is never mistaken for GNSS. Provenance for a projected
  point is `estimated`.
- `attributes.ppe` is populated only if a PPE classifier ran on the crop; otherwise
  `null` (absent, not "no PPE").
- `provenance` at object level is always `observed` — perception does not infer.
- `advisory: true` — the platform-wide invariant travels on every contract.

**Storage:** a `vision_observations` table (hypertable — high volume), write-once,
site-scoped. Raw frames are **not** stored here; only the structured objects (± a clip
ref on a promoted event). Retention is a per-class policy.

**JSON Schema (draft 2020-12) — specified, ready to lift into `contracts/`:**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://eigenstatesystems.com/contracts/vision.observation.v1.json",
  "title": "vision.observation.v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema","site_id","camera_id","ts","frame_id","model","objects","advisory"],
  "properties": {
    "schema": { "const": "vision.observation.v1" },
    "site_id": { "type": "string", "minLength": 1 },
    "camera_id": { "type": "string", "minLength": 1 },
    "ts": { "type": "string", "format": "date-time" },
    "received_at": { "type": ["string","null"], "format": "date-time" },
    "frame_id": { "type": "string", "minLength": 1 },
    "model": {
      "type": "object", "additionalProperties": false,
      "required": ["id","version"],
      "properties": { "id": {"type":"string"}, "version": {"type":"string"} }
    },
    "image": {
      "type": ["object","null"], "additionalProperties": false,
      "properties": { "width": {"type":"integer"}, "height": {"type":"integer"} }
    },
    "objects": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["track_id","class","confidence","bbox","provenance"],
        "properties": {
          "track_id": { "type": "string" },
          "class": { "type": "string", "description": "mining object ontology class" },
          "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
          "bbox": {
            "type": "object", "additionalProperties": false,
            "required": ["x1","y1","x2","y2"],
            "properties": {
              "x1": {"type":"number"}, "y1": {"type":"number"},
              "x2": {"type":"number"}, "y2": {"type":"number"}
            }
          },
          "ground_point": {
            "type": ["object","null"], "additionalProperties": false,
            "properties": {
              "lat": {"type":"number"}, "lon": {"type":"number"},
              "basis": {"type":"string","enum":["homography","depth","unknown"]}
            }
          },
          "attributes": { "type": ["object","null"] },
          "provenance": { "const": "observed" }
        }
      }
    },
    "advisory": { "const": true }
  }
}
```

---

## 2. `vision.operational_event.v1` (interpreted)

```json
{
  "schema": "vision.operational_event.v1",
  "event_id": "01J9ZB...",
  "site_id": "kn-zw-01",
  "camera_id": "cam-rom-01",
  "ts": "2026-09-05T11:43:20Z",
  "type": "loading_complete",
  "subject": {
    "track_id": "cam-rom-01:37",
    "asset_id": "HT-102",
    "association": "probable"
  },
  "zone_id": "r-load-1",
  "provenance": "inferred",
  "confidence": 0.82,
  "evidence": {
    "observation_window": ["2026-09-05T11:41:50Z", "2026-09-05T11:43:20Z"],
    "track_ids": ["cam-rom-01:37", "cam-rom-01:12"],
    "frame_ids": ["...", "..."],
    "signals": { "dwell_s": 78, "proximity_to_excavator_m": 6.4, "min_conf": 0.71 },
    "clip_uri": null
  },
  "advisory": true
}
```

Field notes:
- `type` is the **operational ontology** (`VISION_ARCHITECTURE.md` §4):
  `zone_entry · zone_exit · queueing · loading_start · loading_complete · dumping ·
  unsafe_proximity · restricted_area · congestion · …` — extensible, versioned.
- `subject.asset_id` + `subject.association` (`confirmed|probable|possible|unknown`) come
  from GNSS/vision fusion — **never a silent identity claim** (`VISION_GPS_FUSION.md`).
  `asset_id` is a foreign key to `assets`; vision never creates an asset.
- `provenance` ∈ `inferred | correlated | estimated`; `confidence` ∈ [0,1].
- `evidence` is mandatory and is what makes the event explainable and recomputable — the
  observation window, contributing tracks/frames, the per-signal contributions, and an
  optional `clip_uri` (MinIO).
- **Reproducible:** stored in a derived `vision_operational_events` table; recomputable
  from `vision_observations` when a rule improves (like haul cycles). Never hand-edited.

---

## 3. Promotion to `event.v1` (existing alarm queue — no schema change)

When an operational event warrants a human, the core emits a **plain `event.v1`** — the
existing contract, already vision-aware:

```json
{
  "schema": "event.v1",
  "event_id": "01J9ZB...",
  "site_id": "kn-zw-01",
  "ts": "2026-09-05T11:43:20Z",
  "type": "proximity",
  "severity": "critical",
  "asset_id": "HT-102",
  "zone_id": "r-load-1",
  "source": "vision:cam-rom-01",
  "summary": "Person within 6 m of moving haul truck HT-102 at ROM loading face",
  "detail": { "association": "probable", "confidence": 0.82, "distance_m": 5.6 },
  "evidence": { "vision_operational_event_id": "01J9ZB...", "clip_uri": "s3://.../clip.mp4" },
  "advisory": true,
  "state": "open"
}
```

- `type` uses the existing enum where it fits (`proximity` is already there); new vision
  operational types that need alarms are added to the `event.v1` `type` enum as an
  additive, versioned change (the enum already anticipates growth: *"Later sources add
  more"*).
- `source` = `vision:<camera_id>` (the schema's documented convention).
- `severity` is set by the **rule/zone policy**, not the model — reusing the zone-rule
  machinery (`restricted` → critical, etc.).
- `evidence` links back to the `vision.operational_event.v1` (its id + provenance +
  confidence) and any clip ref — so an operator can always see *why*, and whether it was
  measured, inferred, or correlated.
- Ack/resolve workflow, dedupe, SSE to the dashboard: **all unchanged.** A vision alarm
  is acknowledged exactly like a geofence alarm.

---

## 4. Registration & validation (reuse the platform substrate)

- Both new contracts are added as JSON Schema in `contracts/` and Pydantic models in
  `src/minemonitor/contracts/`, then registered in `platform/contracts.py` — the
  registry that already holds the 7 current contracts. Versions are immutable there
  (re-binding a schema string to a different model is refused).
- The core **validates every inbound `vision.*.v1` against the registry at the ingest
  boundary and rejects malformed or unversioned observations loudly** — the same
  validate-or-reject discipline the position/HTTP ingest already enforces. Never
  silently coerce model output.
- The core may **publish** `vision.operational_event.v1` on the in-process event bus
  (`platform/bus.py`), like `dispatch.recommendation.v1` today, so analytics/notification
  consumers subscribe without new wiring.

## 5. Provenance vocabulary (platform-wide, restated)

| Label | Meaning | Example |
|---|---|---|
| `observed` | a direct model detection | a `haul_truck` box at 0.94 |
| `inferred` | a rule over observations/time | `loading_complete` from dwell + proximity |
| `correlated` | tied to a GNSS asset by fusion | `asset_id` with `association: probable` |
| `estimated` | a measured-adjacent computed value | ground distance from homography |
| `unknown` | insufficient basis | association `unknown`; no ground calibration |

Every derived field carries one of these, plus confidence and evidence where not
`observed`. The dashboard **must render the label** — a camera guess is never shown as a
measurement.

**STOP:** contracts are designed and specified. They are added to `contracts/` and the
registry only on approval, as an additive step alongside the vision build.
