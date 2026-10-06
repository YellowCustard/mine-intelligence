"""Render a release's compose file from the repository's docker-compose.yml.

    python release/render_compose.py sources docker-compose.yml
    python release/render_compose.py render docker-compose.yml images.lock 0.2.0 > out.yml

The repository's compose file builds the Mine Monitor images from source and names third-party
images by tag. A release must run exactly the images it shipped with, and must neither build nor
pull at deploy time (the site may be offline, and an upstream tag can move). So the release file:

- replaces each ``build:`` with the release's prebuilt image (``Dockerfile`` is the core image,
  ``Dockerfile.vision`` the vision edge image);
- replaces every image with the release-local tag recorded in ``images.lock``. That tag carries
  the registry digest the image was pinned to, so two releases never share a mutable tag;
- adds ``pull_policy: never`` to every service: compose never fetches anything, and an image the
  installer did not load is an error rather than a silent download;
- fixes the compose project name, so the data volumes keep their names whichever release
  directory compose runs from.

``sources`` prints what a release has to provide, one per line: each third-party image reference,
then ``build:<dockerfile>`` for each image built from this repository. The release build reads it,
so docker-compose.yml stays the only list of images.

``images.lock`` has one image per line, ``<local ref> <pinned ref> <source>``, where the source is
a line of ``sources``. Blank lines and ``#`` comments are ignored.

This is a line-level transform, so the rendered file keeps its comments for whoever reads it on
the box. Any layout it does not recognise is an error, never passed through unchanged.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

PROJECT_NAME = "minemonitor"

_SERVICE = re.compile(r"^  ([a-z0-9][a-z0-9_-]*):\s*$")
_IMAGE = re.compile(r"^    image:\s*(\S+)\s*$")
_BUILD_INLINE = re.compile(r"^    build:\s*(\S+)\s*$")
_BUILD_BLOCK = re.compile(r"^    build:\s*$")
_BUILD_KEY = re.compile(r"^      (context|dockerfile):\s*(\S+)\s*$")
_PULL_POLICY = re.compile(r"^    pull_policy:")
_TOP_LEVEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:")
# Release-local tags are <repository>:<tag>; the pinned reference is <repository>@sha256:<hex>.
_LOCAL_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_PINNED_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class RenderError(ValueError):
    """The compose file or the lock is not in a shape this renderer understands."""


@dataclass
class _Service:
    name: str
    start: int  # index of the service's header line
    source: str | None = None  # upstream image ref, or build:<dockerfile>
    image_line: int | None = None  # index of the line the image goes on
    drop: list[int] = field(default_factory=list)  # build-block lines to remove


def _scan(lines: list[str]) -> list[_Service]:
    services: list[_Service] = []
    in_services = False
    current: _Service | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TOP_LEVEL.match(line):
            if line.startswith("name:"):
                raise RenderError("docker-compose.yml already sets a project name")
            in_services = line.rstrip() == "services:"
            current = None
            i += 1
            continue
        if not in_services:
            i += 1
            continue
        m = _SERVICE.match(line)
        if m:
            current = _Service(name=m.group(1), start=i)
            services.append(current)
            i += 1
            continue
        if current is None:
            i += 1
            continue
        if _PULL_POLICY.match(line):
            raise RenderError(f"service {current.name} already sets pull_policy")
        source: str | None = None
        if m := _IMAGE.match(line):
            source = m.group(1)
            current.image_line = i
        elif m := _BUILD_INLINE.match(line):
            if m.group(1) != ".":
                raise RenderError(f"service {current.name}: unexpected build context {m.group(1)}")
            source = "build:Dockerfile"
            current.image_line = i
        elif _BUILD_BLOCK.match(line):
            keys: dict[str, str] = {}
            j = i + 1
            while j < len(lines) and lines[j].startswith("      "):
                km = _BUILD_KEY.match(lines[j])
                if not km:
                    raise RenderError(
                        f"service {current.name}: unsupported build setting {lines[j].strip()!r}"
                    )
                keys[km.group(1)] = km.group(2)
                j += 1
            if keys.get("context", ".") != ".":
                raise RenderError(f"service {current.name}: unexpected build context")
            source = "build:" + keys.get("dockerfile", "Dockerfile")
            current.image_line = i
            current.drop.extend(range(i + 1, j))
            i = j - 1
        if source is not None:
            if current.source is not None:
                raise RenderError(f"service {current.name} has both image and build")
            current.source = source
        i += 1
    if not services:
        raise RenderError("no services found")
    for svc in services:
        if svc.source is None:
            raise RenderError(f"service {svc.name} has neither image nor build")
    return services


def sources(compose_text: str) -> list[str]:
    """Every image the compose file needs: third-party references first, then build targets."""
    found = [s.source for s in _scan(compose_text.splitlines()) if s.source]
    upstream = sorted({s for s in found if not s.startswith("build:")})
    builds = sorted({s for s in found if s.startswith("build:")})
    return upstream + builds


def parse_lock(lock_text: str) -> dict[str, tuple[str, str]]:
    """Map each source to its (local ref, pinned ref)."""
    entries: dict[str, tuple[str, str]] = {}
    for n, raw in enumerate(lock_text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise RenderError(f"images.lock line {n}: want '<local ref> <pinned ref> <source>'")
        local, pinned, source = parts
        if not _LOCAL_REF.match(local):
            raise RenderError(f"images.lock line {n}: bad local ref {local}")
        if not _PINNED_REF.match(pinned):
            raise RenderError(f"images.lock line {n}: not pinned to a sha256 digest: {pinned}")
        if source in entries:
            raise RenderError(f"images.lock line {n}: {source} listed twice")
        entries[source] = (local, pinned)
    return entries


def render(compose_text: str, lock_text: str, version: str) -> str:
    """The release compose file: prebuilt, pinned images; never pulls; fixed project name."""
    if not _VERSION.match(version):
        raise RenderError(f"release version must be MAJOR.MINOR.PATCH, got {version!r}")
    lines = compose_text.splitlines()
    services = _scan(lines)
    lock = parse_lock(lock_text)
    missing = sorted({s.source for s in services if s.source not in lock})
    if missing:
        raise RenderError("images.lock has no entry for: " + ", ".join(m or "" for m in missing))
    unused = sorted(set(lock) - {s.source for s in services})
    if unused:
        raise RenderError("images.lock lists images no service uses: " + ", ".join(unused))

    replace: dict[int, list[str]] = {}
    drop: set[int] = set()
    for svc in services:
        assert svc.image_line is not None and svc.source is not None
        local, _pinned = lock[svc.source]
        replace[svc.image_line] = [f"    image: {local}", "    pull_policy: never"]
        drop.update(svc.drop)

    out: list[str] = []
    header_done = False
    for i, line in enumerate(lines):
        if not header_done and not line.startswith("#"):
            out += [
                f"# Release {version}: rendered from docker-compose.yml"
                " by release/render_compose.py.",
                "# Every image is pinned (images.lock), and compose never pulls or builds.",
                "# Do not edit: an upgrade replaces this file. Settings belong in .env.",
                "",
                f"name: {PROJECT_NAME}",
            ]
            header_done = True
        if i in drop:
            continue
        out += replace.get(i, [line])
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "sources":
            with open(argv[1], encoding="utf-8") as f:
                print("\n".join(sources(f.read())))
            return 0
        if len(argv) == 4 and argv[0] == "render":
            with open(argv[1], encoding="utf-8") as f:
                compose_text = f.read()
            with open(argv[2], encoding="utf-8") as f:
                lock_text = f.read()
            sys.stdout.write(render(compose_text, lock_text, argv[3]))
            return 0
    except (OSError, RenderError) as exc:
        print(f"render_compose: {exc}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
