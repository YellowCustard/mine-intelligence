"""The opt-in evidence object store: its fail-closed start script and its compose lockdown.

The store itself (SeaweedFS) is not run here; docs/OBJECT_STORAGE.md records the interop and
lockdown checks made against the release binary. These tests pin what would silently regress:
an open or default-credentialed store, a store back in the default ``docker compose up``, or a
SeaweedFS admin port reachable from other containers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "objectstore-start.sh"
COMPOSE = ROOT / "docker-compose.yml"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")

GOOD_ENV = {
    "MM_OBJECTSTORE_ADMIN_KEY": "mm-admin",
    "MM_OBJECTSTORE_ADMIN_SECRET": "admin-secret-0123456789",
    "MM_EDGE_S3_ACCESS_KEY": "edge-01",
    "MM_EDGE_S3_SECRET_KEY": "edge+secret/0123456789=",
    "MM_EDGE_S3_BUCKET": "mine-evidence",
}


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    config = tmp_path / "s3.json"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MM_OBJECTSTORE", "MM_EDGE"))}
    proc = subprocess.run(
        ["sh", str(SCRIPT), str(config), "sh", "-c", "echo started"],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, config


def test_renders_two_least_privilege_identities_then_execs(tmp_path: Path) -> None:
    proc, config = _run(tmp_path, GOOD_ENV)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "started"  # exec'd the given command
    assert config.stat().st_mode & 0o777 == 0o600  # secrets are not world-readable
    ids = {i["name"]: i for i in json.loads(config.read_text())["identities"]}
    assert set(ids) == {"admin", "edge"}  # no anonymous identity
    assert ids["edge"]["actions"] == ["Write:mine-evidence"]
    assert ids["edge"]["credentials"] == [
        {"accessKey": "edge-01", "secretKey": "edge+secret/0123456789="}
    ]
    assert "Admin" in ids["admin"]["actions"]


@pytest.mark.parametrize(
    "var",
    [
        "MM_OBJECTSTORE_ADMIN_KEY",
        "MM_OBJECTSTORE_ADMIN_SECRET",
        "MM_EDGE_S3_ACCESS_KEY",
        "MM_EDGE_S3_SECRET_KEY",
    ],
)
def test_missing_credential_refuses_to_start(tmp_path: Path, var: str) -> None:
    proc, config = _run(tmp_path, {k: v for k, v in GOOD_ENV.items() if k != var})
    assert proc.returncode == 1
    assert var in proc.stderr
    assert "started" not in proc.stdout
    assert not config.exists()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MM_OBJECTSTORE_ADMIN_SECRET", "short-secret"),  # < 16
        ("MM_EDGE_S3_SECRET_KEY", 'a"},{"name":"anonymous","x":"1234'),  # JSON injection
        ("MM_EDGE_S3_SECRET_KEY", "has a space 0123456789"),
        ("MM_EDGE_S3_ACCESS_KEY", "ab"),  # < 3
        ("MM_OBJECTSTORE_ADMIN_KEY", "k\\0123"),
        ("MM_EDGE_S3_SECRET_KEY", "x" * 129),  # > 128
    ],
)
def test_unsafe_credential_refuses_to_start(tmp_path: Path, var: str, value: str) -> None:
    proc, config = _run(tmp_path, {**GOOD_ENV, var: value})
    assert proc.returncode == 1
    assert var in proc.stderr
    assert not config.exists()


def test_edge_and_admin_keys_must_differ(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, {**GOOD_ENV, "MM_EDGE_S3_ACCESS_KEY": "mm-admin"})
    assert proc.returncode == 1
    assert "must differ" in proc.stderr


@pytest.mark.parametrize(
    "bucket", ["Mine-Evidence", "ab", "-evidence", "evidence-", "mine_evidence"]
)
def test_invalid_bucket_refuses_to_start(tmp_path: Path, bucket: str) -> None:
    proc, _ = _run(tmp_path, {**GOOD_ENV, "MM_EDGE_S3_BUCKET": bucket})
    assert proc.returncode == 1
    assert "MM_EDGE_S3_BUCKET" in proc.stderr


def test_usage_error_without_a_command(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["sh", str(SCRIPT), str(tmp_path / "s3.json")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2


# -- compose ------------------------------------------------------------------------------


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())["services"]
    return services


def test_object_store_is_opt_in_and_nothing_default_needs_it() -> None:
    services = _services()
    for name in ("objectstore", "objectstore-init"):
        assert services[name]["profiles"] == ["evidence"]
    for name, svc in services.items():
        assert "minio" not in str(svc).lower(), name
        if not svc.get("profiles"):  # started by a plain `docker compose up`
            deps = svc.get("depends_on", {})
            assert "objectstore" not in deps, name


def test_object_store_is_locked_down() -> None:
    svc = _services()["objectstore"]
    image = svc["image"]
    assert image.startswith("chrislusf/seaweedfs:")
    assert image.split(":", 1)[1][0].isdigit()  # an exact release, never a floating tag
    cmd = svc["command"]
    for flag in (
        "-master.telemetry=false",  # upstream default posts usage stats off-site
        "-ip.bind=127.0.0.1",  # master/volume/filer APIs are unauthenticated
        "-s3.ip.bind=0.0.0.0",
        "-s3.port.iceberg=0",
        "-s3.port.lance=0",
        "-s3.config=/tmp/s3.json",
    ):
        assert flag in cmd, flag
    assert svc["entrypoint"][:4] == [
        "/bin/sh",
        "/mm/objectstore-start.sh",
        "/tmp/s3.json",
        "/entrypoint.sh",
    ]
    assert svc["ports"] == ["${MM_OBJECTSTORE_BIND:-127.0.0.1}:8333:8333"]
    assert svc["networks"] == ["evidence"]


def test_only_the_edge_node_shares_the_evidence_network() -> None:
    services = _services()
    on_evidence = {
        name for name, svc in services.items() if "evidence" in (svc.get("networks") or [])
    }
    assert on_evidence == {"objectstore", "objectstore-init", "vision-edge"}
    env = services["vision-edge"]["environment"]
    assert env["MM_EDGE_S3_ENDPOINT"] == "${MM_EDGE_S3_ENDPOINT:-http://objectstore:8333}"
