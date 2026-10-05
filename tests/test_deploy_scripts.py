"""deploy.sh and restore.sh turn broker auth on from .env alone (finding D1).

Run against stub ``docker``, ``curl`` and ``sleep`` commands that log each call, so the order of
the compose steps can be checked without a Docker daemon. The stub answers the two questions the
scripts put to the containers: the API's MM_MQTT_USERNAME (what compose resolved from the shell
and .env), and what the broker says to a client with no login.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case "$*" in
  "compose exec -T api printenv MM_MQTT_USERNAME")
    printf '%s\n' "$STUB_MQTT_USER" ;;
  "compose exec -T mqtt mosquitto_pub "*)
    case "$STUB_BROKER" in
      auth) echo "Connection error: Connection Refused: not authorised." >&2; exit 5 ;;
      anonymous) exit 0 ;;
      down) echo "Error: Connection refused" >&2; exit 1 ;;
    esac ;;
  "compose exec -T db psql "*)
    cat > /dev/null ;;
esac
exit 0
"""

BROKER_FILES = "compose exec -T api uv run python -m minemonitor.devices.broker_config"
ANON_PROBE = "compose exec -T mqtt mosquitto_pub -h 127.0.0.1 -t healthcheck -m ok"
FULL_UP = "compose up -d --build"
API_UP = "compose up -d --build db api"


def _run(
    tmp_path: Path, script: str, *, user: str, broker: str = "auth", args: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    # The scripts only check .env exists (restore.sh also sources it); compose is stubbed.
    (repo / ".env").write_text(f"MM_MQTT_USERNAME={user}\n")
    (repo / "dump.sql").write_text("-- empty\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": _DOCKER,
        "curl": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_MQTT_USER": user,
        "STUB_BROKER": broker,
    }
    # No MM_MQTT_USERNAME in the script's own environment: the login lives in .env only.
    result = subprocess.run(
        ["bash", str(repo / "deploy" / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
        timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _index(calls: list[str], call: str) -> int:
    assert call in calls, f"{call!r} not run; calls: {calls}"
    return calls.index(call)


@pytest.mark.parametrize("script", ["deploy.sh", "restore.sh"])
def test_login_in_env_file_generates_broker_files_before_the_broker_starts(
    tmp_path: Path, script: str
) -> None:
    args = ("dump.sql",) if script == "restore.sh" else ()
    result, calls = _run(tmp_path, script, user="mm-svc", args=args)
    assert result.returncode == 0, result.stdout + result.stderr
    api_up = _index(calls, API_UP)
    files = _index(calls, BROKER_FILES)
    full_up = _index(calls, FULL_UP)
    probe = _index(calls, ANON_PROBE)
    assert api_up < files < full_up < probe, calls
    assert "Broker refuses anonymous clients." in result.stdout


@pytest.mark.parametrize("script", ["deploy.sh", "restore.sh"])
def test_no_login_runs_the_dev_broker_without_broker_files(tmp_path: Path, script: str) -> None:
    args = ("dump.sql",) if script == "restore.sh" else ()
    result, calls = _run(tmp_path, script, user="", broker="anonymous", args=args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert FULL_UP in calls
    assert BROKER_FILES not in calls
    assert ANON_PROBE not in calls


@pytest.mark.parametrize("script", ["deploy.sh", "restore.sh"])
def test_fails_when_the_broker_lets_anonymous_clients_in(tmp_path: Path, script: str) -> None:
    args = ("dump.sql",) if script == "restore.sh" else ()
    result, _ = _run(tmp_path, script, user="mm-svc", broker="anonymous", args=args)
    assert result.returncode != 0
    assert "accepted a client with no login" in result.stdout
    assert "MM_MOSQUITTO_CONF" in result.stdout


@pytest.mark.parametrize("script", ["deploy.sh", "restore.sh"])
def test_fails_when_the_broker_never_answers(tmp_path: Path, script: str) -> None:
    args = ("dump.sql",) if script == "restore.sh" else ()
    result, calls = _run(tmp_path, script, user="mm-svc", broker="down", args=args)
    assert result.returncode != 0
    assert "docker compose logs mqtt" in result.stdout
    assert calls.count(ANON_PROBE) > 1  # retried while the broker starts


def test_restore_loads_the_dump_before_starting_the_api(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "restore.sh", user="mm-svc", args=("dump.sql",))
    assert result.returncode == 0, result.stdout + result.stderr
    load = next(i for i, c in enumerate(calls) if c.startswith("compose exec -T db psql "))
    assert calls.index("compose up -d db") < load < _index(calls, API_UP)
