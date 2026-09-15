# Mine Monitor — Vision Edge Deployment

**Status: architecture for review. No edge service or dependency has been built.**

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
        │  vision.observation.v1 / vision.operational_event.v1  (thin JSON, ± clip ref)
        ▼  MQTT  mm/<site>/vision/<cam>
 Mine Monitor core (unchanged; ingests, fuses, alarms, stores)
```

- **Raw video never leaves the edge.** Only structured events, and clip *references*
  (clip bytes to MinIO under retention) cross. Bandwidth + data-protection.
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

Rules:
- **Default is CPU-small.** A GPU is an upgrade, never a requirement to run.
- Multiple cameras on one node share the GPU/CPU budget — frame rate is allocated per
  camera to stay within the headroom targets (`VISION_ARCHITECTURE.md` §12), dropping
  *old* frames under load, never blocking.
- If the GPU is unavailable at runtime, the node falls back to the CPU profile and logs a
  degraded state — a tested failure mode, not a crash.

## 4. Configuration, identity, and security (reuse, don't reinvent)

- **Broker identity:** the edge node authenticates to Mosquitto as a **`device`-role**
  principal with a per-node secret, ACL-confined to `mm/<site>/vision/<cam>` — the exact
  device-credential model already built for trackers (`devices/`, per-device `$7$`
  secret + ACL). No new auth surface.
- **Camera config** (RTSP URL/credentials, image-zone → operational-zone mapping,
  homography calibration, per-zone thresholds) is data, admin-managed via `/api/v1`,
  **audited** like zone/rule changes. Camera credentials are secrets, never logged.
- **Model rollout:** the node pulls its ONNX artifact + registry metadata for the version
  pinned to its site; a model change is a config/version change, audited, reversible by
  status rollback. Production weights are never overwritten in place.
- **Retention:** frames/clips are a new retention data class extending `retention.py`;
  the edge ring buffer is bounded; MinIO holds only referenced evidence, deletable per
  site.

## 5. Failure modes (designed for, and tested — `VISION_ARCHITECTURE.md` §13)

- camera offline / stream drop → reconnect with backoff; emit an `asset_offline`-style
  camera-health signal, don't spin.
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

**STOP:** deployment topology and backends are specified; no edge image, dependency, or
model is built. Real-hardware benchmarking happens at commissioning and is recorded in
the registry (the same "validated on real hardware, not mocks" posture the platform
already takes).
