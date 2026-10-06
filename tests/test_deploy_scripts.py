"""deploy.sh and restore.sh: broker auth from .env alone (finding D1), the TimescaleDB
extension update after a db image change, and the database dump every deploy ends with.

Run against stub ``docker``, ``curl`` and ``sleep`` commands that log each call, so the order of
the compose steps can be checked without a Docker daemon. The stub answers the two questions the
scripts put to the containers: the API's MM_MQTT_USERNAME (what compose resolved from the shell
and .env), what the broker says to a client with no login, and what the database says to
``ALTER EXTENSION timescaledb UPDATE``.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]

_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
if [ "$1 $2 $3 $4 $5 $6" = "compose exec -T db sh -c" ] && [ "$#" -ge 8 ]; then
  case "${@: -1}" in
    "ALTER EXTENSION timescaledb UPDATE")
      case "$STUB_TS" in
        fresh) echo 'ERROR:  extension "timescaledb" does not exist' >&2; exit 1 ;;
        current)
          echo 'NOTICE:  version "2.30.2" of extension "timescaledb" is already installed' >&2 ;;
        broken) echo 'ERROR:  could not access file "$libdir/timescaledb-2.30.2"' >&2; exit 1 ;;
      esac
      echo "ALTER EXTENSION" ;;
    "SELECT extversion "*) echo "2.30.2" ;;
  esac
  exit 0
fi
case "$*" in
  "compose exec -T api python -c "*MM_MQTT_USERNAME*)
    [ "$STUB_LOOKUP" = fail ] && { echo "service \"api\" is not running" >&2; exit 1; }
    printf '%s\n' "$STUB_MQTT_USER" ;;
  "compose exec -T mqtt mosquitto_pub "*)
    case "$STUB_BROKER" in
      auth) echo "Connection error: Connection Refused: not authorised." >&2; exit 5 ;;
      anonymous) exit 0 ;;
      down) echo "Error: Connection refused" >&2; exit 1 ;;
    esac ;;
  "compose exec -T db psql "*)
    cat > /dev/null ;;
  "compose exec -T db pg_dump "*)
    if [ "$STUB_DUMP" = fail ]; then
      echo "-- partial"; echo "pg_dump: error: connection lost" >&2; exit 1
    fi
    echo "-- dump" ;;
esac
exit 0
"""

BROKER_FILES = "compose exec -T api uv run python -m minemonitor.devices.broker_config"
ANON_PROBE = "compose exec -T mqtt mosquitto_pub -h 127.0.0.1 -t healthcheck -m ok"
FULL_UP = "compose up -d --build"
API_UP = "compose up -d --build db api"
DB_UP = "compose up -d db"
TS_UPDATE = "mm_db_sql ALTER EXTENSION timescaledb UPDATE"
DUMP = "compose exec -T db pg_dump -U minemonitor minemonitor"
TERMINATE = "pg_terminate_backend"


def _run(
    tmp_path: Path,
    script: str,
    *,
    user: str,
    broker: str = "auth",
    timescale: str = "current",
    lookup: str = "ok",
    dump: str = "ok",
    args: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
    stubs: dict[str, str] | None = None,
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
        **(stubs or {}),
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
        "STUB_TS": timescale,
        "STUB_LOOKUP": lookup,
        "STUB_DUMP": dump,
        **(env_extra or {}),
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


@pytest.mark.parametrize("script", ["deploy.sh", "restore.sh"])
def test_stops_when_the_login_cannot_be_read(tmp_path: Path, script: str) -> None:
    # A failed lookup must not read as "no broker login": that would skip the broker files and
    # the anonymous-client check, and start whatever broker config compose picked.
    args = ("dump.sql",) if script == "restore.sh" else ()
    result, calls = _run(tmp_path, script, user="mm-svc", lookup="fail", args=args)
    assert result.returncode != 0
    assert "could not read MM_MQTT_USERNAME" in result.stderr
    assert FULL_UP not in calls
    assert BROKER_FILES not in calls


def test_restore_loads_the_dump_before_starting_the_api(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "restore.sh", user="mm-svc", args=("dump.sql",))
    assert result.returncode == 0, result.stdout + result.stderr
    load = next(i for i, c in enumerate(calls) if c.startswith("compose exec -T db psql "))
    assert calls.index("compose up -d db") < load < _index(calls, API_UP)


def _find(calls: list[str], fragment: str) -> list[int]:
    return [i for i, c in enumerate(calls) if fragment in c]


def test_deploy_updates_an_old_timescaledb_extension_before_the_api_starts(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, "deploy.sh", user="", broker="anonymous", timescale="old")
    assert result.returncode == 0, result.stdout + result.stderr
    (update,) = _find(calls, TS_UPDATE)
    # Its own session with no .psqlrc, so nothing loads the old library first.
    assert calls[update].startswith("compose exec -T db sh -c psql -X ")
    (terminate,) = _find(calls, TERMINATE)
    assert _index(calls, DB_UP) < update < terminate < _index(calls, API_UP), calls
    assert "Updated the TimescaleDB extension to 2.30.2" in result.stdout


@pytest.mark.parametrize("state", ["current", "fresh"])
def test_deploy_leaves_sessions_alone_when_there_is_nothing_to_update(
    tmp_path: Path, state: str
) -> None:
    # current: already at the image's version. fresh: not migrated yet, so the API's first
    # migration creates the extension at the image's version.
    result, calls = _run(tmp_path, "deploy.sh", user="", broker="anonymous", timescale=state)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _find(calls, TS_UPDATE)
    assert not _find(calls, TERMINATE)
    assert "Updated the TimescaleDB extension" not in result.stdout


def test_deploy_stops_when_the_extension_update_fails(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "deploy.sh", user="", broker="anonymous", timescale="broken")
    assert result.returncode != 0
    assert "could not update the TimescaleDB extension" in result.stdout
    assert API_UP not in calls


def _dumps(tmp_path: Path) -> list[Path]:
    backups = tmp_path / "repo" / "backups"
    return sorted(backups.iterdir()) if backups.exists() else []


def test_deploy_ends_with_a_dump(tmp_path: Path) -> None:
    # Dumps restore only onto the TimescaleDB version they were taken on, so each deploy takes one.
    result, calls = _run(tmp_path, "deploy.sh", user="mm-svc")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _index(calls, ANON_PROBE) < _index(calls, DUMP), calls
    (dump,) = _dumps(tmp_path)
    assert re.fullmatch(r"minemonitor_\d{8}_\d{6}_after-deploy\.sql\.gz", dump.name)
    assert gzip.decompress(dump.read_bytes()) == b"-- dump\n"
    # Personal data: the owner alone can read the dump or list the directory.
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert stat.S_IMODE(dump.parent.stat().st_mode) == 0o700


def test_deploy_fails_loudly_when_its_dump_fails_and_leaves_no_file(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "deploy.sh", user="mm-svc", dump="fail")
    assert result.returncode != 0
    assert DUMP in calls
    assert "the database dump after the deploy failed" in result.stdout
    assert "pg_dump: error" in result.stderr
    assert _dumps(tmp_path) == []  # no truncated file under a dump's name


def test_deploy_no_backup_skips_the_dump(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "deploy.sh", user="mm-svc", args=("--no-backup", "--demo"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert DUMP not in calls
    assert "compose --profile sim up -d --build simulator" in calls
    assert "no database dump after this deploy" in result.stdout


def test_deploy_under_the_installer_leaves_the_dump_to_it(tmp_path: Path) -> None:
    # deploy/mmctl dumps once the release has verified, not before.
    result, calls = _run(tmp_path, "deploy.sh", user="mm-svc", env_extra={"MM_INSTALLER": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert DUMP not in calls


def test_deploy_rejects_an_unknown_argument(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "deploy.sh", user="mm-svc", args=("--dmeo",))
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert calls == []


def test_backup_refuses_to_fill_the_disk(tmp_path: Path) -> None:
    backups = tmp_path / "repo" / "backups"
    backups.mkdir(parents=True)
    # Named by hand (bash deploy/backup.sh <file>): it still counts towards the estimate.
    (backups / "before-migration.sql.gz").write_bytes(b"x" * 300 * 1024)
    df = (
        "#!/bin/sh\n"
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        "echo '/dev/x 2000000 1000000 1000000 50% /'\n"
    )
    result, calls = _run(tmp_path, "backup.sh", user="", stubs={"df": df})
    assert result.returncode != 0
    assert "976 MB free" in result.stdout and "needs 1024 MB" in result.stdout, result.stdout
    assert DUMP not in calls
    assert _dumps(tmp_path) == [backups / "before-migration.sql.gz"]


def _backup_service_script(backups: Path) -> str:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    (script,) = compose["services"]["backup"]["command"]
    return script.replace("$$", "$").replace("/backups", str(backups))


@pytest.mark.parametrize("pg_dump", ["ok", "fail"])
def test_backup_service_never_leaves_a_truncated_dump(tmp_path: Path, pg_dump: str) -> None:
    # gzip exits 0 whatever pg_dump did, so without pipefail a failed dump looked written.
    backups = tmp_path / "backups"
    backups.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "pg_dump": "#!/bin/sh\necho '-- dump'\n"
        + ("echo 'pg_dump: error' >&2; exit 1\n" if pg_dump == "fail" else ""),
        "sleep": "#!/bin/sh\nkill -TERM $PPID\n",  # one pass of the loop
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", _backup_service_script(backups)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "MM_BACKUP_KEEP_DAYS": "14"},
        check=False,
        timeout=30,
    )
    files = [p.name for p in backups.iterdir()]
    if pg_dump == "ok":
        assert "backup written" in result.stdout
        (name,) = files
        assert name.endswith(".sql.gz")
        assert stat.S_IMODE((backups / name).stat().st_mode) == 0o600
    else:
        assert "backup FAILED" in result.stdout
        assert files == []
