# Licence ledger

Every library, model and SDK that ships gets logged here with its exact licence
and source. **Apache-2.0 / MIT / BSD only.** AGPL is a commercial licensing trap
for a product we sell.

> **Ultralytics YOLO is AGPL-3.0 and must not enter this codebase or Mine Monitor
> Vision.** Model licences vary per model and per release, not per vendor — check
> each one individually.

## Python runtime dependencies

| Package | Licence | Source |
|---|---|---|
| fastapi | MIT | https://github.com/fastapi/fastapi |
| starlette | BSD-3-Clause | https://github.com/encode/starlette |
| uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
| pydantic | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | MIT | https://github.com/pydantic/pydantic-settings |
| SQLAlchemy | MIT | https://github.com/sqlalchemy/sqlalchemy |
| alembic | MIT | https://github.com/sqlalchemy/alembic |
| psycopg (3) | LGPL-3.0 | https://github.com/psycopg/psycopg |
| python-ulid | MIT | https://github.com/mdomke/python-ulid |
| paho-mqtt | EPL-2.0 / EDL-1.0 (dual) | https://github.com/eclipse/paho.mqtt.python |

> **paho-mqtt** is dual-licensed EPL-2.0 / EDL-1.0. We take it under **EDL-1.0**,
> which is the BSD-3-Clause text — permissive and compatible with a product we
> sell. It is imported unmodified.

> **psycopg 3** is LGPL-3.0. It is used unmodified as a dynamically linked
> library (the driver), which LGPL permits in a proprietary product; we do not
> modify or statically embed it. If that ever changes, revisit.

## Vision edge dependencies (optional `vision` extra — edge node only)

Installed only on a vision edge node (`uv sync --extra vision`); the core and its image never
need them. Selection and the full per-model audit: `docs/VISION_MODEL_LICENSES.md`.

| Package | Licence | Source |
|---|---|---|
| numpy | BSD-3-Clause | https://github.com/numpy/numpy |
| onnxruntime | MIT | https://github.com/microsoft/onnxruntime |
| opencv-python-headless | MIT (packaging) / Apache-2.0 (OpenCV); bundles FFmpeg under LGPL-2.1 | https://github.com/opencv/opencv-python |
| flatbuffers (via onnxruntime) | Apache-2.0 | https://github.com/google/flatbuffers |
| protobuf (via onnxruntime) | BSD-3-Clause | https://github.com/protocolbuffers/protobuf |

> **FFmpeg inside opencv-python-headless** is the LGPL-2.1 build shipped as shared libraries in
> the wheel, used unmodified for decoding. A GPL FFmpeg build must never enter the edge image.

### Vision model weights (fetched with a pinned SHA-256 — never committed)

| Model | Code + weights licence | Source | SHA-256 |
|---|---|---|---|
| YOLOX-Nano (ONNX, release 0.1.1rc0) | Apache-2.0 | https://github.com/Megvii-BaseDetection/YOLOX | `c789161e…d18a0b7d` |
| YOLOX-S (ONNX, release 0.1.1rc0) | Apache-2.0 | https://github.com/Megvii-BaseDetection/YOLOX | `c5c2d13e…fe998063` |

> **No Ultralytics.** YOLOX is Megvii's own Apache-2.0 detector, not an Ultralytics package. The
> model registry refuses any AGPL/GPL/non-commercial model at any status.

## Development-only dependencies (not shipped to the mine)

| Package | Licence | Source |
|---|---|---|
| pytest | MIT | https://github.com/pytest-dev/pytest |
| httpx | BSD-3-Clause | https://github.com/encode/httpx |
| jsonschema | MIT | https://github.com/python-jsonschema/jsonschema |
| ruff | MIT | https://github.com/astral-sh/ruff |
| uv | Apache-2.0 / MIT | https://github.com/astral-sh/uv |

## Infrastructure images (docker-compose)

| Image | Licence | Source |
|---|---|---|
| timescale/timescaledb (2.30.2-pg16) | Apache-2.0 (Timescale Community for some features) | https://github.com/timescale/timescaledb |
| eclipse-mosquitto | EPL-2.0 / EDL-1.0 | https://github.com/eclipse/mosquitto |
| chrislusf/seaweedfs (4.48, opt-in `evidence` profile) | Apache-2.0 (the open-source edition; the separate SeaweedFS Enterprise edition is not used) | https://github.com/seaweedfs/seaweedfs |
| python (base image) | PSF | https://hub.docker.com/_/python |

> **MinIO was removed (2026-10).** It is AGPL-3.0 and its community images are
> no longer published, so a clean `docker compose up` could not pull it. The
> evidence object store is now SeaweedFS (Apache-2.0), run unmodified as an
> opt-in service and reached only over the S3 HTTP API; our edge uploader is a
> stdlib SigV4 client (`vision/edge/s3.py`), so no SeaweedFS code is linked.
> SeaweedFS's anonymous usage telemetry is on by default upstream and is
> disabled here (`-master.telemetry=false`). See `docs/OBJECT_STORAGE.md`.
