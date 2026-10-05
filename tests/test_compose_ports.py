"""Published ports: loopback by default, unless the port is meant to face the network.

Docker writes its own iptables rules for published ports, so ufw does not filter them. The bind
address in docker-compose.yml is the only thing that keeps the database and the broker off the
mine LAN, so a port published without one (``"5432:5432"`` binds every interface) must fail here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"

# (service, container port) that must be reachable from other hosts, and why.
NETWORK_FACING = {
    ("caddy", "80"),  # TLS front door: HTTP->HTTPS redirect and ACME challenges
    ("caddy", "443"),
    ("teltonika", "5027"),  # trackers dial in; narrowed with MM_TELTONIKA_BIND
}

_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _render(value: str) -> str:
    """Compose interpolation with nothing set, i.e. every ``${VAR:-default}`` at its default."""
    return _VAR.sub(lambda m: m.group(2) or "", value)


def _published() -> list[tuple[str, str]]:
    services: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())["services"]
    return [(name, port) for name, svc in services.items() for port in svc.get("ports", [])]


def _bind_and_target(port: str) -> tuple[str, str]:
    parts = _render(port).split(":")
    assert len(parts) in (2, 3), port
    return (parts[0] if len(parts) == 3 else "0.0.0.0"), parts[-1]


def test_ports_use_the_short_string_syntax() -> None:
    # The long (mapping) syntax would slip past the checks below.
    assert _published()
    for name, port in _published():
        assert isinstance(port, str), (name, port)


def test_internal_ports_bind_loopback_by_default() -> None:
    for name, port in _published():
        bind, target = _bind_and_target(port)
        if (name, target) in NETWORK_FACING:
            continue
        assert bind == "127.0.0.1", f"{name} publishes {port} on {bind}"


def test_internal_ports_are_opened_only_by_a_documented_bind_setting() -> None:
    documented = set(re.findall(r"^#?\s*(MM_[A-Z_]+_BIND)=", ENV_EXAMPLE.read_text(), re.M))
    for name, port in _published():
        if name == "caddy":  # the public front door; no bind setting by design
            continue
        m = re.match(r"^\$\{(MM_[A-Z_]+_BIND):-[^}]+\}:", port)
        assert m, f"{name} {port}: the host address must come from an MM_*_BIND setting"
        assert m.group(1) in documented, f"{m.group(1)} is not documented in .env.example"


def test_database_and_broker_stay_off_the_network() -> None:
    bound = {name: _bind_and_target(port) for name, port in _published()}
    assert bound["db"] == ("127.0.0.1", "5432")
    assert bound["mqtt"] == ("127.0.0.1", "1883")
