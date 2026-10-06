"""Release packaging: the compose file a release ships, and the version a tag must match.

The installer itself (deploy/mmctl) is exercised end to end on real Docker by
release/test-install.sh in CI; these tests pin down the pure parts.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

_spec = importlib.util.spec_from_file_location("render_compose", ROOT / "release/render_compose.py")
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
sys.modules["render_compose"] = rc
_spec.loader.exec_module(rc)

DIGEST = "sha256:" + "ab" * 32


def _lock(sources: list[str]) -> str:
    lines = ["# a comment", ""]
    for s in sources:
        if s == "build:Dockerfile":
            repo, tag = "ghcr.io/yellowcustard/mine-monitor", "1.2.3"
        elif s == "build:Dockerfile.vision":
            repo, tag = "ghcr.io/yellowcustard/mine-monitor-vision-edge", "1.2.3"
        else:
            repo, tag = s.rsplit(":", 1)
        lines.append(f"{repo}:{tag}-abababababab {repo}@{DIGEST} {s}")
    return "\n".join(lines) + "\n"


def _services(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return doc["services"]


def test_sources_lists_every_image_once_third_party_first() -> None:
    sources = rc.sources(COMPOSE)
    assert sources[-2:] == ["build:Dockerfile", "build:Dockerfile.vision"]
    assert len(sources) == len(set(sources))
    source_doc = yaml.safe_load(COMPOSE)
    upstream = {svc["image"] for svc in _services(source_doc).values() if "image" in svc}
    assert set(sources[:-2]) == upstream


def test_render_pins_every_service_and_changes_nothing_else() -> None:
    sources = rc.sources(COMPOSE)
    lock = rc.parse_lock(_lock(sources))
    out = rc.render(COMPOSE, _lock(sources), "1.2.3")
    src_doc = yaml.safe_load(COMPOSE)
    out_doc = yaml.safe_load(out)

    assert out_doc["name"] == rc.PROJECT_NAME
    for name, svc in _services(out_doc).items():
        src_svc = _services(src_doc)[name]
        if "build" in src_svc:
            build = src_svc["build"]
            dockerfile = "Dockerfile" if build == "." else build.get("dockerfile", "Dockerfile")
            source = f"build:{dockerfile}"
        else:
            source = src_svc["image"]
        assert svc["image"] == lock[source][0], name
        assert svc["pull_policy"] == "never", name
        assert "build" not in svc, name
        # Everything else is the repository's compose file, unchanged.
        rest = {k: v for k, v in svc.items() if k not in ("image", "pull_policy")}
        src_rest = {k: v for k, v in src_svc.items() if k not in ("image", "build")}
        assert rest == src_rest, name
    stripped = copy.deepcopy(out_doc)
    del stripped["name"]
    assert set(stripped) == set(src_doc)
    for key in set(src_doc) - {"services"}:
        assert stripped[key] == src_doc[key]


def test_render_keeps_the_comments() -> None:
    out = rc.render(COMPOSE, _lock(rc.sources(COMPOSE)), "1.2.3")
    assert "Docker-published ports bypass ufw" in out
    assert out.startswith("# Mine Monitor")


def test_render_refuses_an_incomplete_or_unpinned_lock() -> None:
    sources = rc.sources(COMPOSE)
    full = _lock(sources)
    missing = "\n".join(line for line in full.splitlines() if not line.endswith(" caddy:2"))
    with pytest.raises(rc.RenderError, match="no entry for: caddy:2"):
        rc.render(COMPOSE, missing, "1.2.3")
    with pytest.raises(rc.RenderError, match="not pinned"):
        rc.render(COMPOSE, full.replace(f"caddy@{DIGEST}", "caddy:2"), "1.2.3")
    with pytest.raises(rc.RenderError, match="no service uses"):
        rc.render(COMPOSE, full + f"redis:7-abababababab redis@{DIGEST} redis:7\n", "1.2.3")
    with pytest.raises(rc.RenderError, match="listed twice"):
        rc.parse_lock(full + full.splitlines()[-1] + "\n")
    with pytest.raises(rc.RenderError, match="MAJOR.MINOR.PATCH"):
        rc.render(COMPOSE, full, "1.2")


@pytest.mark.parametrize(
    ("compose", "error"),
    [
        ("name: x\nservices:\n  a:\n    image: a:1\n", "already sets a project name"),
        ("services:\n  a:\n    image: a:1\n    pull_policy: always\n", "already sets pull_policy"),
        ("services:\n  a:\n    image: a:1\n    build: .\n", "both image and build"),
        ("services:\n  a:\n    restart: always\n", "neither image nor build"),
        ("services:\n  a:\n    build: ./other\n", "unexpected build context"),
        (
            "services:\n  a:\n    build:\n      context: .\n      target: dev\n",
            "unsupported build setting",
        ),
        ("volumes:\n  v:\n", "no services"),
    ],
)
def test_render_rejects_layouts_it_does_not_understand(compose: str, error: str) -> None:
    with pytest.raises(rc.RenderError, match=error):
        rc.render(compose, "", "1.2.3")


def test_release_version_is_one_value() -> None:
    """The release workflow requires the tag to equal both; they must agree with each other."""
    from minemonitor import __version__

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__)


def test_bundle_ships_the_files_the_installer_needs() -> None:
    """build-bundle.sh copies these; a rename here must not silently drop one from a release."""
    script = (ROOT / "release/build-bundle.sh").read_text(encoding="utf-8")
    for path in [
        ".env.example",
        "LICENCES.md",
        "docker/Caddyfile",
        "docker/mosquitto.conf",
        "docker/mosquitto.auth.conf",
        "docker/objectstore-start.sh",
        "deploy/mmctl",
        "deploy/INSTALL.md",
        "release/install.sh",
        "docs/OPERATIONS.md",
    ]:
        assert (ROOT / path).is_file(), path
        assert path in script, path
    # Every config file the compose file mounts from ./docker ships in the bundle.
    for mount in re.findall(r"\./docker/([A-Za-z0-9_.-]+\.(?:conf|sh)|Caddyfile)", COMPOSE):
        assert f"docker/{mount}" in script, mount
