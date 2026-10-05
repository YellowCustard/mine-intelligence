"""The broker's config follows the same login setting its clients use (finding D1).

Every client in the stack gets MM_MQTT_USERNAME from compose, which reads the shell first and then
.env. The broker's config used to be chosen by a separate variable that only deploy.sh exported,
and only when the login was in deploy.sh's own environment. A site that put the login in .env, as
commissioning step A2 says to, ran the anonymous broker, and so did every later
``docker compose up`` on any site. These tests render the real compose file the way Docker does,
and run the mqtt healthcheck against a real broker.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
BROKER_CONF_TARGET = "/mosquitto/config/mosquitto.conf"


def _have_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return probe.returncode == 0


needs_compose = pytest.mark.skipif(not _have_compose(), reason="docker compose not installed")
needs_mosquitto = pytest.mark.skipif(
    shutil.which("mosquitto") is None or shutil.which("mosquitto_pub") is None,
    reason="mosquitto and mosquitto_pub not installed",
)


def _render(tmp_path: Path, env_file: str, shell: dict[str, str]) -> dict[str, Any]:
    """``docker compose config`` with only ``env_file`` and ``shell`` as the environment."""
    (tmp_path / "test.env").write_text(env_file)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MM_", "COMPOSE_"))}
    env.update(shell)
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(tmp_path / "test.env"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    rendered: dict[str, Any] = json.loads(out.stdout)
    return rendered


def _broker_conf(rendered: dict[str, Any]) -> str:
    mounts = [
        v for v in rendered["services"]["mqtt"]["volumes"] if v["target"] == BROKER_CONF_TARGET
    ]
    assert len(mounts) == 1, mounts
    return str(Path(mounts[0]["source"]).relative_to(ROOT))


@needs_compose
@pytest.mark.parametrize(
    ("env_file", "shell", "expected"),
    [
        pytest.param("", {}, "docker/mosquitto.conf", id="no-login"),
        pytest.param("MM_MQTT_USERNAME=\n", {}, "docker/mosquitto.conf", id="blank-login"),
        pytest.param(
            "MM_MQTT_USERNAME=mm-svc\n", {}, "docker/mosquitto.auth.conf", id="login-in-env-file"
        ),
        pytest.param(
            "", {"MM_MQTT_USERNAME": "mm-svc"}, "docker/mosquitto.auth.conf", id="login-in-shell"
        ),
        pytest.param(
            "MM_MQTT_USERNAME=mm-svc\nMM_MOSQUITTO_CONF=./docker/mosquitto.conf\n",
            {},
            "docker/mosquitto.conf",
            id="explicit-override",
        ),
    ],
)
def test_broker_config_follows_the_login(
    tmp_path: Path, env_file: str, shell: dict[str, str], expected: str
) -> None:
    rendered = _render(tmp_path, env_file, shell)
    assert _broker_conf(rendered) == expected


@needs_compose
def test_broker_and_clients_read_the_same_login(tmp_path: Path) -> None:
    rendered = _render(tmp_path, "MM_MQTT_USERNAME=mm-svc\n", {})
    for client in ("api", "ingestor"):
        assert rendered["services"][client]["environment"]["MM_MQTT_USERNAME"] == "mm-svc"
    assert _broker_conf(rendered) == "docker/mosquitto.auth.conf"


def test_auth_config_refuses_anonymous_clients() -> None:
    text = (ROOT / "docker/mosquitto.auth.conf").read_text()
    lines = [line.split() for line in text.splitlines()]
    settings = {words[0]: words[1:] for words in lines if words and not words[0].startswith("#")}
    assert settings["allow_anonymous"] == ["false"]
    assert settings["password_file"] == ["/mosquitto/config/auth/passwd"]
    assert settings["acl_file"] == ["/mosquitto/config/auth/acl"]


# --- the mqtt healthcheck, run against a real broker ---------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _healthcheck(port: int) -> int:
    """Run the compose healthcheck command against ``port``; return its exit code."""
    test = yaml.safe_load(COMPOSE.read_text())["services"]["mqtt"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    command = test[1].replace("$$", "$")  # compose's escape for a literal $
    target = "mosquitto_pub -h 127.0.0.1 "
    assert command.count(target) == 1, command
    command = command.replace(target, f"mosquitto_pub -h 127.0.0.1 -p {port} ")
    return subprocess.run(["sh", "-c", command], capture_output=True, check=False).returncode


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Any]:
    procs: list[subprocess.Popen[bytes]] = []

    def start(*, anonymous: bool) -> int:
        port = _free_port()
        (tmp_path / "passwd").write_text("")
        (tmp_path / "acl").write_text("")
        conf = tmp_path / f"mosquitto-{port}.conf"
        # Run as the current user so mosquitto can read these files (it drops root otherwise).
        auth = (
            "allow_anonymous true\n"
            if anonymous
            else f"allow_anonymous false\npassword_file {tmp_path / 'passwd'}\n"
            f"acl_file {tmp_path / 'acl'}\n"
        )
        conf.write_text(f"listener {port} 127.0.0.1\nuser {getpass.getuser()}\n{auth}")
        procs.append(subprocess.Popen(["mosquitto", "-c", str(conf)], stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return port
            except OSError:
                time.sleep(0.1)
        pytest.fail("mosquitto did not start")

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)


@needs_mosquitto
def test_healthcheck_passes_on_the_authenticated_broker(broker: Any) -> None:
    # The broker turns the anonymous probe away with "not authorised", which only a live broker
    # can send. Before, the probe's refusal marked every authenticated broker unhealthy.
    assert _healthcheck(broker(anonymous=False)) == 0


@needs_mosquitto
def test_healthcheck_passes_on_the_anonymous_broker(broker: Any) -> None:
    assert _healthcheck(broker(anonymous=True)) == 0


@needs_mosquitto
def test_healthcheck_fails_when_no_broker_answers() -> None:
    assert _healthcheck(_free_port()) != 0
