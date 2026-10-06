#!/usr/bin/env bash
# Assemble a Mine Monitor release bundle from images already in the local Docker image store.
#
#   bash release/build-bundle.sh --version 0.2.0 \
#     --core-image ghcr.io/yellowcustard/mine-monitor@sha256:... \
#     --vision-image ghcr.io/yellowcustard/mine-monitor-vision-edge@sha256:... \
#     --out dist
#
# Run from the repository root, after pulling every third-party image docker-compose.yml names
# (`python3 release/render_compose.py sources docker-compose.yml`) and the two Mine Monitor images.
# Writes to --out:
#
#   mine-monitor-<version>.tar.gz                  online bundle: pulls the pinned images itself
#   mine-monitor-<version>-linux-amd64-offline.tar offline bundle: the images are inside
#   SHA256SUMS                                     checksums of both
#
# Each image is pinned to its registry digest and tagged locally as <repository>:<tag>-<digest12>,
# so a tag can never point two releases at different content. --local-images accepts images that
# were never pushed (CI): their pin is the local image id, so only the offline bundle can install.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="" CORE="" VISION="" OUT="" LOCAL=0
CORE_REPO=ghcr.io/yellowcustard/mine-monitor
VISION_REPO=ghcr.io/yellowcustard/mine-monitor-vision-edge
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION=$2; shift 2 ;;
    --core-image) CORE=$2; shift 2 ;;
    --vision-image) VISION=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --local-images) LOCAL=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$VERSION" ] || [ -z "$CORE" ] || [ -z "$VISION" ] || [ -z "$OUT" ]; then
  echo "usage: build-bundle.sh --version X.Y.Z --core-image REF --vision-image REF --out DIR [--local-images]" >&2
  exit 2
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be MAJOR.MINOR.PATCH: $VERSION" >&2; exit 2; }

repo_of() {  # repository part of an image reference (drops @digest, then :tag)
  local r=${1%%@*}
  case "${r##*/}" in *:*) r=${r%:*} ;; esac
  printf '%s' "$r"
}
tag_of() {
  local r=${1%%@*}
  case "${r##*/}" in *:*) printf '%s' "${r##*:}" ;; *) printf 'latest' ;; esac
}

# The registry digest the image was pulled or pushed by: the RepoDigest for its own repository.
# Docker Hub images report short names (caddy@sha256:...), so compare without docker.io/library/.
pinned_digest() {
  local ref=$1 want d
  want=$(repo_of "$ref"); want=${want#docker.io/}; want=${want#library/}
  while read -r d; do
    [ -n "$d" ] || continue
    local repo=${d%@*}; repo=${repo#docker.io/}; repo=${repo#library/}
    if [ "$repo" = "$want" ]; then printf '%s' "${d#*@}"; return 0; fi
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref")
  if [ "$LOCAL" = 1 ]; then
    docker image inspect --format '{{.Id}}' "$ref"
    return 0
  fi
  echo "ERROR: $ref has no registry digest for $(repo_of "$ref"); pull or push it first" >&2
  return 1
}

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
NAME="mine-monitor-$VERSION"
B="$STAGE/$NAME"
mkdir -p "$B/docker" "$B/deploy" "$B/docs" "$OUT"

echo "==> Pinning images"
{
  echo "# Mine Monitor $VERSION images: <local ref> <pinned ref> <source in docker-compose.yml>"
  echo "# The installer loads (offline bundle) or pulls the pinned ref and tags it as the local ref."
} > "$B/images.lock"
LOCALS=()
while read -r source; do
  case "$source" in
    build:Dockerfile) ref=$CORE; repo=$CORE_REPO; tag=$VERSION ;;
    build:Dockerfile.vision) ref=$VISION; repo=$VISION_REPO; tag=$VERSION ;;
    build:*) echo "ERROR: no image for $source" >&2; exit 1 ;;
    *) ref=$source; repo=$(repo_of "$source"); tag=$(tag_of "$source") ;;
  esac
  docker image inspect "$ref" >/dev/null 2>&1 || { echo "ERROR: image not in the local store: $ref" >&2; exit 1; }
  digest=$(pinned_digest "$ref")
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: bad digest for $ref: $digest" >&2; exit 1; }
  local_ref="$repo:$tag-${digest:7:12}"
  docker tag "$ref" "$local_ref"
  printf '%s %s %s\n' "$local_ref" "$repo@$digest" "$source" >> "$B/images.lock"
  LOCALS+=("$local_ref")
  echo "    $source -> $local_ref"
done < <(python3 release/render_compose.py sources docker-compose.yml)

echo "==> Rendering the release compose file"
python3 release/render_compose.py render docker-compose.yml "$B/images.lock" "$VERSION" > "$B/docker-compose.yml"

cp .env.example LICENCES.md "$B/"
cp docker/Caddyfile docker/mosquitto.conf docker/mosquitto.auth.conf docker/objectstore-start.sh "$B/docker/"
cp deploy/*.sh deploy/*.md deploy/mmctl "$B/deploy/"
cp docs/OPERATIONS.md docs/OBJECT_STORAGE.md docs/VISION_DEPLOYMENT.md "$B/docs/"
cp release/install.sh "$B/install.sh"
cp deploy/INSTALL.md "$B/README.md"
chmod 0755 "$B/install.sh" "$B/deploy/mmctl" "$B"/deploy/*.sh
{
  echo "MM_RELEASE_VERSION=$VERSION"
  echo "MM_RELEASE_ARCH=amd64"
  echo "MM_RELEASE_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "MM_RELEASE_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$B/RELEASE"

checksums() {
  rm -f "$B/SHA256SUMS"
  (cd "$B" && find . -type f -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum) > "$STAGE/SHA256SUMS"
  mv "$STAGE/SHA256SUMS" "$B/SHA256SUMS"
}

echo "==> Writing the online bundle"
checksums
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -czf "$OUT/$NAME.tar.gz" "$NAME"

echo "==> Saving ${#LOCALS[@]} images for the offline bundle"
docker save "${LOCALS[@]}" | gzip -6 > "$B/images.tar.gz"
checksums
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -cf "$OUT/$NAME-linux-amd64-offline.tar" "$NAME"

(cd "$OUT" && sha256sum "$NAME.tar.gz" "$NAME-linux-amd64-offline.tar" > SHA256SUMS)
ls -l "$OUT"
