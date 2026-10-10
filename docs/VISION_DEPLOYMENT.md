# Mine Monitor — Vision Edge Deployment

**Status: Stage C pilot readiness built (vision V5, 2026-10-01).** The edge node service, live
RTSP source, `Dockerfile.vision`, the compose `vision` profile, the node's broker identity and
evidence stills exist and are tested (§7 records the container smoke). **Deploying it on site still
waits on the Stage C gate** (an approved edge box and camera — `VISION_BUILD_GATE.md`). §8 is the
commissioning runbook for that day.

Covers Phase 17 (edge deployment) and Phase 18 (service boundary), consistent with the
edge-service ruling in `PLATFORM_EVOLUTION_ARCHITECTURE.md` §2.1 and the boundary in
`VISION_ARCHITECTURE.md` §9.

## 1. Where vision runs

Vision is a **separate edge process/service**, deployed **next to the cameras** on the
mine LAN — not in the core service, not in the cloud.

```
 IP cameras (RTSP/ONVIF, mine LAN)
        │
        ▼
 ┌─────────────────────────────────────────────┐
 │ Vision Edge Node  (own container/process)    │
 │  decode → detect → track → spatial/temporal  │
 │  → mining activity engine                    │
 │  raw frames/clips: LOCAL ring buffer only    │
 └─────────────────────────────────────────────┘
        │  vision.operational_event.v1 / zone_rollup / health  (thin JSON, ± evidence ref)
        ▼  HTTP POST /api/v1/ingest/vision   or   MQTT mm/<site>/vision/<cam|node>/<kind>
 Mine Monitor core (unchanged; ingests, fuses, alarms, stores)
```

- **Raw video never leaves the edge.** Only structured events, and clip *references*
  (clip bytes to the evidence object store under retention) cross. Bandwidth + data-protection.
- The edge node is its own container in the **same `docker compose`** model — the mine
  runs one `docker compose up`, vision included, on a box that may or may not have a GPU.
  It is a profile/service, not a separate cloud stack.
- **Independent failure:** if the edge node is down, the core is unaffected (GNSS,
  geofencing, alarms, analytics continue). Vision degrades to "no vision", never takes
  the core with it.

## 2. Inference backends (choose by real hardware, never assume a GPU)

The **same `vision.*.v1` interface and the same operational rules run on every backend** —
only the adapter's inference path differs (`VISION_ARCHITECTURE.md` §5).

| Backend | Licence | Target hardware | Role |
|---|---|---|---|
| **PyTorch** | BSD-3 | dev / training box | development, training, reference inference |
| **ONNX Runtime** | MIT | anything (CPU/GPU) | **default portable** production backend |
| **OpenVINO** | Apache-2.0 | Intel CPU / iGPU | CPU-small edge optimisation |
| **TensorRT** | NVIDIA EULA | NVIDIA GPU (Jetson/T4/RTX/L4) | GPU-balanced/accuracy; EULA-respected, not vendored into the repo |

Export path: train in PyTorch → export **ONNX** (the portable artifact stored in the
registry) → optionally compile to TensorRT/OpenVINO **on the edge box for its hardware**.
The ONNX artifact is the source of truth; hardware-specific engines are build outputs,
not checked-in weights.

## 3. Deployment profiles

The profile is a config choice; the interface and events are identical across them.

| Profile | Hardware | Model | Backend | ~fps/cam | Cameras | Use |
|---|---|---|---|---|---|---|
| **CPU-small** | 4–8 core mini-PC, no GPU | YOLOX-Nano/S | OpenVINO / ONNX | 2–5 | 1–2 | MVP, quiet zones |
| **GPU-balanced** | Jetson Orin / T4-class | RT-DETRv2-R50 | TensorRT | 15–30 | several | active pit zones |
| **GPU-accuracy** | RTX A4000+ / L4-class | RT-DETRv2 (larger) + optional seg | TensorRT | 30+ | several+ | proximity safety, busy scenes |

**Measured (vision V3, 2026-09-29) — CPU-small, 4 cores, ONNX Runtime CPU, official YOLOX
ONNX weights.** `python -m minemonitor.vision.edge.cli bench` over 100 decoded 1280×720 frames:

| Model | Input | Inference fps | Latency p50 / p95 | Note |
|---|---|---|---|---|
| YOLOX-Nano | 416×416 | 96.8 | 10.0 / 11.8 ms | default edge detector — ample headroom for several cameras at 5 fps |
| YOLOX-S | 640×640 | 10.6 | 85.8 / 137.5 ms | higher-accuracy option on the same box, fewer cameras |

Latency and throughput are content-independent and carry over to site footage. **Detection
accuracy does not**: these runs used a synthetic evaluation clip (a public-domain portrait panned
across a plain background), so they say nothing about recall at distance, at night or in dust — that
is measured on consented site footage (`VISION_TRAINING.md` §4) before any C/D decision. Real edge
hardware is re-benchmarked on commissioning and recorded in the model registry.

Rules:
- **Default is CPU-small.** A GPU is an upgrade, never a requirement to run.
- Multiple cameras on one node share the GPU/CPU budget — frame rate is allocated per
  camera to stay within the headroom targets (`VISION_ARCHITECTURE.md` §12), dropping
  *old* frames under load, never blocking.
- If the GPU is unavailable at runtime, the node falls back to the CPU profile and logs a
  degraded state — a tested failure mode, not a crash.

## 4. Configuration, identity, and security (reuse, don't reinvent)

- **API identity (default transport):** the node is a `device`-role user scoped to its site
  (`python -m minemonitor.auth.cli edge-01 device --site <site>`). It pulls
  `GET /vision/config` and posts to `POST /ingest/vision` with it. When a device row of the same
  id exists (below), HTTP holds the account to it too: vision records for its own site only.
- **Broker identity (MQTT transport, optional):** a device row with `source: "vision-edge"` and
  `asset_id` = the node id (`POST /sites/<site>/devices`, one-time secret). The generated ACL
  lets it write `mm/<site>/vision/#` **for its own site only** — never a position topic — and
  it can never authorise tracker positions. The ingestor additionally rejects a record whose
  topic and payload disagree. Same `$7$` secret + ACL model as trackers; no new auth surface.
- **Camera config split:** the *scene* (image-zone → operational-zone mapping, homography) is
  data in the core, admin-managed via `PUT .../cameras/<id>/scene` and **audited**. The *stream
  URL* carries the camera password, so it lives only in the node's environment
  (`MM_EDGE_STREAMS`), is never sent to the core, and is redacted (`user:***@`) in every log
  line.
- **Model rollout:** the node pulls its ONNX artifact + registry metadata for the version
  pinned to its site; a model change is a config/version change, audited, reversible by
  status rollback. Production weights are never overwritten in place.
- **Evidence:** one JPEG still per operational event (the confirming frame), referenced by
  `evidence.clip_ref` — never inline bytes; the scheme is allow-listed (`edge://`, `s3://`,
  `nvr://`). `MM_EDGE_EVIDENCE=edge` (default) keeps stills only in the node's bounded ring
  (`MM_EDGE_EVIDENCE_MAX_ITEMS` / `_MAX_AGE_DAYS`); `s3` also forwards them, store-and-forward,
  to the evidence object store (SeaweedFS, compose profile `evidence`) under
  `<site>/vision/<camera>/<date>/` so a site's evidence is exportable and deletable by prefix
  (set an S3 lifecycle rule for its retention); `off` keeps none. Stills
  show people: personal data, never biometric templates.

## 5. Failure modes (designed for, and tested — `VISION_ARCHITECTURE.md` §13)

- camera offline / stream drop → reconnect with capped backoff (1 s → 60 s, jitter); after
  `MM_EDGE_NO_SIGNAL_AFTER_S` (15 s) the camera reports `no_signal` with no frame time, and the
  core raises one `camera_offline` once that lasts `MM_VISION_CAMERA_STALE_S`. A *frozen
  picture* (identical frames, which a static scene under heavy compression can mimic) is a
  visible camera condition, not an offline alarm.
- corrupted/partial frame → skip frame, count it, continue.
- model unavailable / GPU unavailable → CPU fallback or degraded-state; never crash the
  node.
- inference timeout / high CPU / dropped frames → shed load by dropping oldest frames,
  keep latency bounded.
- **core unreachable** → the edge node **buffers observations locally and backfills on
  reconnect** — same store-and-forward guarantee as the position spool (M2: "no positions
  lost or duplicated"). Out-of-order/late observations are tolerated and re-derivable.
- duplicate observation → idempotent ingest at the core (dedupe by
  `site/cam/frame/track` + event id), never double-counts.

## 6. Boundary guarantee (Phase 18)

The core must **not know or care** which model produced an observation — YOLOX, RT-DETR,
SAM, or a future model. It validates `vision.*.v1` against the contract registry and
proceeds. This is the long-term maintainability contract: perception is replaceable and
upgradeable behind a stable event boundary, without a core rewrite.

## 7. What V5 built, and how it was verified

| Piece | Where | Behaviour |
|---|---|---|
| Live source | `vision/edge/rtsp.py` | grabber thread per camera; FFmpeg over TCP with a socket timeout; keeps only the newest sampled frame (drop-old-not-recent, counted); fixed-cadence sampling at `MM_EDGE_SAMPLE_FPS` with jitter tolerance; capped backoff + jitter; credentials redacted |
| Node service | `vision/edge/node.py` | all cameras, one model, one crash-safe outbox; config fetched and cached (`config.json`), boots offline from the cache; a refresh rebuilds only a camera whose scene/rules changed (tracks and the open rollup carry over when only rules change); node health every heartbeat (`degraded` while a camera is down or running from cache, `down` on clean stop); SIGTERM → flush + final drain |
| Transport | `vision/edge/publish.py` | HTTP (default) or MQTT QoS 1 to `mm/<site>/vision/<camera_or_node>/<kind>` (node waits for the broker connection rather than queueing in memory) |
| Evidence | `vision/edge/evidence.py`, `vision/edge/s3.py` | one still per alarm; bounded edge ring; optional S3 upload via a stdlib SigV4 client (checked against AWS's worked example and SeaweedFS 4.48) |
| Image | `Dockerfile.vision` | `python:3.11-slim` + the `vision` extra, non-root, no weights (mounted at `/models`), heartbeat healthcheck; ≈826 MB |
| Compose | `docker-compose.yml` service `vision-edge`, profile `vision` | opt-in like `trackers`; state on the `vision_state` volume |

**Container smoke (2026-10-01, 4-core CPU, the image above, real YOLOX-Nano).** The core API ran
on the host; the evaluation clip (`cli make-still-clip`) was served as a **live, password-protected
RTSP stream** (MediaMTX, test tool only). Observed:

- The node started before the camera: retries at 1.2 → 1.9 → 4.2 → 8.3 → 14 → 29.7 s, then
  connected through RTSP auth. Every log line showed `rtsp://viewer:***@…`; the camera password
  appeared **0 times** in the container log.
- A person walking into the restricted image-zone raised **one** `zone_breach` (critical,
  `source vision:<cam>`) with an `edge://` evidence reference; the JPEG was on the state volume.
- Steady state: **5.09 fps analysed** at `MM_EDGE_SAMPLE_FPS=5`, ~0.73 of one core, ~97 MiB RAM,
  outbox depth 0, container `healthy`.
- Stream killed: the camera reported `no_signal` and the node `degraded` ("stream did not open")
  within seconds, retrying from ~1 s again. Stream restored: reconnected. `docker stop`: clean
  shutdown, final `down` health delivered, nothing left pending.

Two defects were found by this smoke and fixed with regression tests: sampling scheduled from
arrival time halved the rate when the camera sent exactly `sample_fps` (2.9 → 5.0 fps); and a
static scene's frozen picture would have become a `camera_offline` alarm (now only "no frames"
does). One ~25 s dip in frames coincided with the test stream's FFmpeg loop restart; it was not
reproduced in other loops and is attributed to the test stream, not the node.

**Not verified here:** a real Dahua camera and the real edge box (Stage C), detection accuracy on
site footage, the `s3` evidence mode inside the compose `evidence` profile (the client is verified
against a signature-checking server and against the SeaweedFS 4.48 release binary run with the same
flags; see `docs/OBJECT_STORAGE.md`), and the
node in MQTT mode end to end through the ingestor (the broker ACL and QoS 1 publish are tested
against a real Mosquitto).

## 8. Commissioning runbook (Stage C pilot — when a box and a camera are approved)

**Before site.**
1. Edge box: 4–8 cores, Docker, NTP-synchronised clock (frame times come from it), on the camera
   VLAN with a route to the core (or the core on the same box).
2. Weights, on any machine with internet: `python -m minemonitor.vision.edge.cli fetch-models`
   → `var/models/` (SHA-256 pinned; a mismatch is deleted, not used). Copy to the box.
3. Build: `docker compose --profile vision build vision-edge`.

**Camera.** Use the **substream** (H.264, ~720p, 10–15 fps); 5 fps is analysed. Create a
read-only camera account for the node, never the admin account. Confirm the RTSP URL plays from
the box (`ffprobe` or VLC) before involving the node.

**Core side** (admin):
1. Register the camera: `POST /api/v1/sites/<site>/cameras` → its id.
2. Install the vision rules once per site: `POST /api/v1/sites/<site>/vision/rules/defaults`.
3. Draw the scene on a snapshot of the camera's view: polygons in normalised image coordinates
   (0–1, origin top-left), each mapped to an existing operational zone:
   `PUT /api/v1/sites/<site>/cameras/<id>/scene` with `image_zones` (and a `homography` only once
   calibrated). The foot point of a person is what is tested against the polygon — draw zones
   on the ground.
4. Node identity: `MM_NEW_USER_PASSWORD=… python -m minemonitor.auth.cli edge-01 device --site
   <site>`. For MQTT transport also `POST /api/v1/sites/<site>/devices` with
   `{"device_id": "edge-01", "asset_id": "edge-01", "source": "vision-edge"}`, then regenerate
   the broker files (`python -m minemonitor.devices.broker_config`) and `kill -HUP 1` the broker.

**Node.** `MM_EDGE_CORE_URL` must be `https://…` when the core is on another host; the node
refuses plain `http` except for localhost or the compose service name, because its device password
rides every request (with Caddy's internal CA, mount the root certificate and set `SSL_CERT_FILE`).
In `.env`: `MM_EDGE_SITE_ID`, `MM_EDGE_NODE_ID`, `MM_EDGE_CORE_URL`,
`MM_EDGE_USERNAME`/`MM_EDGE_PASSWORD`, and `MM_EDGE_STREAMS={"<camera id>": "rtsp://…"}`.
Start: `docker compose --profile vision up -d vision-edge` (on a separate edge box add
`--no-deps`, so only the node runs there). Expect in `docker compose logs
vision-edge`: `edge node running` (with `config_source: core`), then `camera stream opened`; and
`docker compose ps` → `healthy` within ~2 min.

**On-site acceptance** — each observed, not assumed:

| # | Do | Expect |
|---|---|---|
| 1 | Nothing in view for 10 min | Vision panel: condition OK, 0 people; `GET .../vision/report` `coverage_pct` ≈ 100; no alarms |
| 2 | Walk into the restricted zone and stay 5 s | exactly **one** critical alarm, `vision:<cam> · inferred`; a still on the node |
| 3 | Walk along the zone boundary | **no** alarm |
| 4 | Two people in view | people-in-view 2 on the panel; rollup peak 2 |
| 5 | Cover the lens | condition → obstructed/dark; no person alarms from the dark |
| 6 | Unplug the camera | `no_signal` within ~15 s, node `degraded`; one `camera_offline` after `MM_VISION_CAMERA_STALE_S`; reconnects by itself when plugged back |
| 7 | Cut the node's link to the core during test 2 | the alarm is raised locally and delivered on reconnect, once; outbox depth returns to 0 |
| 8 | Power-cycle the node while the core is down | node boots from the cached config (`config_source: cache`, `degraded`) and keeps detecting |
| 9 | `python -m minemonitor.vision.edge.cli bench --model yolox-nano --clip <recorded clip>` on the box | record fps / p50 / p95 in the model registry (`POST /api/v1/vision/models/<id>/<version>/metrics`) |

**Tune, then record.** On a static view, confirm the camera condition stays `ok`: heavy
compression can make a still scene look frozen. If it reads `no_signal` with frames arriving,
lower `MM_EDGE_FROZEN_DIFF` (mean absolute pixel change counted as "identical", default 0.5) or
raise `MM_EDGE_FROZEN_FRAMES` (default 25). Record the acceptance results, the camera model and firmware, and the benchmark in
the commissioning log (`deploy/COMMISSIONING.md`).

**Data protection, before go-live:** the mine (controller) confirms signage/consent for camera
analytics in the covered area; choose `MM_EDGE_EVIDENCE` (`edge` keeps stills on the node only);
set the evidence bucket's S3 lifecycle rule if `s3` is used. No face template is produced or stored anywhere in
this path.
