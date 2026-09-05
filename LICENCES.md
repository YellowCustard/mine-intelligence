# Licence ledger

Every library, model and SDK that ships gets logged here with its exact licence
and source. **Apache-2.0 / MIT / BSD only.** AGPL is a commercial licensing trap
for a product we sell.

> **Ultralytics YOLO is AGPL-3.0 and must not enter this codebase or the vision
> repo.** Model licences vary per model and per release, not per vendor — check
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
| timescale/timescaledb | Apache-2.0 (Timescale Community for some features) | https://github.com/timescale/timescaledb |
| eclipse-mosquitto | EPL-2.0 / EDL-1.0 | https://github.com/eclipse/mosquitto |
| minio/minio | AGPL-3.0 (used as an unmodified standalone service over its S3 API — not linked into our code) | https://github.com/minio/minio |
| python (base image) | PSF | https://hub.docker.com/_/python |

> **MinIO is AGPL-3.0.** It is run as a separate, unmodified network service
> accessed only over the S3 HTTP API; our code does not link against or modify
> it, so the AGPL's source-distribution obligation is confined to MinIO itself.
> This is the same posture the mine's own S3-compatible storage would take. If
> we ever fork or embed MinIO, this must be revisited — flag before doing so.
