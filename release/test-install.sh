#!/usr/bin/env bash
# End-to-end test of the release installer on this Docker host. CI runs it on every change.
#
#   sudo CORE_V1=<core image at version V1> CORE_V2=<core image at version V2> V1=0.1.0 V2=0.1.1 \
#     bash release/test-install.sh
#
# Builds release bundles from the two images, then on this host: a fresh install from the offline
# bundle into an empty image store, an upgrade, a rollback, a failed upgrade that rolls itself
# back, backup and restore, and a fresh install from a dump.
#
# DESTRUCTIVE: removes every container, image and volume on the host, and $MMH. CI only.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${CORE_V1:?} ${CORE_V2:?} ${V1:?} ${V2:?}"
V3="${V2%.*}.$((${V2##*.} + 1))"
WORK=${WORK:-$(mktemp -d)}
export MM_HOME=${MMH:-/opt/mine-monitor}
MMCTL="$MM_HOME/current/deploy/mmctl"
mkdir -p "$WORK"

step() { echo; echo "######## $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
expect() {  # description actual wanted
  [ "$2" = "$3" ] || fail "$1: got '$2', want '$3'"
  echo "ok: $1 = $3"
}
sql() { docker exec "$(docker ps -q --filter label=com.docker.compose.project=minemonitor --filter label=com.docker.compose.service=db)" psql -U minemonitor -d minemonitor -tAc "$1"; }
users() { sql "select string_agg(username, ',' order by username) from users"; }
current() { basename "$(readlink -f "$MM_HOME/current")"; }
https_status() { curl -sk -o /dev/null -w '%{http_code}' ${1:+-u "$1"} https://localhost/me; }
new_user() {
  "$MMCTL" compose exec -T -e MM_NEW_USER_PASSWORD=test-user-password api \
    uv run python -m minemonitor.auth.cli "$1" viewer >/dev/null 2>&1
}
unpack() {  # archive -> prints the bundle dir
  local d
  d="$WORK/unpacked-$(basename "$1")"
  rm -rf "$d"
  mkdir -p "$d"
  tar -C "$d" -xf "$1"
  printf '%s' "$d"/mine-monitor-*
}

step "Build bundles: $V1 and $V2 (offline), $V3 (online only; $V2's images under a new version)"
python3 release/render_compose.py sources docker-compose.yml | grep -v '^build:' | while read -r ref; do
  docker image inspect "$ref" >/dev/null 2>&1 || docker pull -q "$ref"
done
# The vision edge service is off in this test, so the core image stands in for its image.
bash release/build-bundle.sh --version "$V1" --core-image "$CORE_V1" --vision-image "$CORE_V1" --local-images --out "$WORK/v1" >/dev/null
bash release/build-bundle.sh --version "$V2" --core-image "$CORE_V2" --vision-image "$CORE_V2" --local-images --out "$WORK/v2" >/dev/null
bash release/build-bundle.sh --version "$V3" --core-image "$CORE_V2" --vision-image "$CORE_V2" --local-images --out "$WORK/v3" >/dev/null
rm -f "$WORK"/v*/mine-monitor-"$V3"-linux-amd64-offline.tar
ls -l "$WORK"/v*/
(cd "$WORK/v1" && sha256sum -c SHA256SUMS)

step "Empty the host: no images, containers or volumes, so the install needs no registry"
docker ps -aq | xargs -r docker rm -f >/dev/null
docker system prune -af --volumes >/dev/null
docker volume prune -af >/dev/null
rm -rf "$MM_HOME"
expect "images on the host" "$(docker images -q | wc -l)" 0

step "Fresh install of $V1 from the offline bundle"
B1=$(unpack "$WORK/v1/mine-monitor-$V1-linux-amd64-offline.tar")
bash "$B1/install.sh" --yes --demo --hostname localhost
expect "running release" "$(current)" "$V1"
expect ".env mode" "$(stat -c %a "$MM_HOME/shared/.env")" 600
expect "MM_ENV" "$(sed -n 's/^MM_ENV=//p' "$MM_HOME/shared/.env")" prod
ADMIN_PW=$(sed -n 's/^password: //p' "$MM_HOME/shared/initial-admin-password")
[ "${#ADMIN_PW}" -ge 20 ] || fail "no generated admin password"
expect "HTTPS login as admin" "$(https_status "admin:$ADMIN_PW")" 200
expect "HTTPS without login" "$(https_status)" 401
expect "HTTPS with a wrong password" "$(https_status "admin:wrong-password")" 401
n=0
for _ in $(seq 1 30); do
  n=$(sql 'select count(*) from positions')
  [ "$n" -gt 0 ] && break
  sleep 2
done
[ "$n" -gt 0 ] || fail "the simulated fleet's positions never arrived"
echo "ok: $n positions stored through the authenticated broker"
[ -s "$MM_HOME/shared/mosquitto/passwd" ] || fail "broker password file missing from shared/"
rm -rf "$B1"

step "Upgrade $V1 -> $V2"
B2=$(unpack "$WORK/v2/mine-monitor-$V2-linux-amd64-offline.tar")
before=$(sql 'select count(*) from positions')
bash "$B2/install.sh" --yes
expect "running release" "$(current)" "$V2"
expect "rollback target" "$(sed -n 's/^MM_ROLLBACK_FROM=//p' "$MM_HOME/shared/rollback.env")" "$V1"
expect "HTTPS login after the upgrade" "$(https_status "admin:$ADMIN_PW")" 200
[ "$(sql 'select count(*) from positions')" -ge "$before" ] || fail "positions lost in the upgrade"
ls "$MM_HOME"/shared/backups/*_v"$V2".sql.gz >/dev/null || fail "no post-upgrade dump"

step "Roll back $V2 -> $V1: data written after the upgrade goes"
new_user after-upgrade
expect "users before the rollback" "$(users)" "admin,after-upgrade"
"$MMCTL" rollback --yes
expect "running release" "$(current)" "$V1"
expect "users after the rollback" "$(users)" admin
expect "HTTPS login after the rollback" "$(https_status "admin:$ADMIN_PW")" 200
[ ! -e "$MM_HOME/shared/rollback.env" ] || fail "rollback record left behind"
zcat "$MM_HOME"/shared/backups/*before-rollback-v"$V2".sql.gz | grep -q after-upgrade || fail "the pre-rollback dump lacks the discarded data"

step "Upgrade to $V2 again, from the installed images"
bash "$B2/install.sh" --yes
expect "running release" "$(current)" "$V2"

step "A failed upgrade to $V3 rolls itself back to $V2"
# $V3's bundle carries $V2's code, so /version answers $V2 and verification fails after deploying.
while read -r ref _ src; do
  case "$ref" in \#* | "") continue ;; esac
  docker tag "$(awk -v s="$src" '$3 == s { print $1 }' "$MM_HOME/releases/$V2/images.lock")" "$ref"
done < <(tar -xzOf "$WORK/v3/mine-monitor-$V3.tar.gz" "mine-monitor-$V3/images.lock")
new_user before-failed-upgrade
B3=$(unpack "$WORK/v3/mine-monitor-$V3.tar.gz")
if bash "$B3/install.sh" --yes; then fail "the $V3 upgrade reported success"; fi
expect "running release" "$(current)" "$V2"
expect "users after the automatic rollback" "$(users)" "admin,before-failed-upgrade"
expect "HTTPS login after the automatic rollback" "$(https_status "admin:$ADMIN_PW")" 200
ls "$MM_HOME"/shared/logs/upgrade-"$V3"-failed-*.log >/dev/null || fail "no log of the failed release"
rm -rf "$B3"

step "Backup and restore"
"$MMCTL" backup "$WORK/dump.sql.gz"
new_user after-dump
"$MMCTL" restore "$WORK/dump.sql.gz" --yes
expect "users after the restore" "$(users)" "admin,before-failed-upgrade"
expect "HTTPS login after the restore" "$(https_status "admin:$ADMIN_PW")" 200

step "Fresh install of $V2 from a dump on a clean box, without HTTPS"
"$MMCTL" compose --profile '*' down --volumes >/dev/null 2>&1
docker volume rm minemonitor_db_rollback >/dev/null 2>&1 || true
rm -rf "$MM_HOME"
bash "$B2/install.sh" --yes --no-tls --restore "$WORK/dump.sql.gz"
expect "running release" "$(current)" "$V2"
expect "restored users" "$(users)" "admin,before-failed-upgrade"
[ ! -e "$MM_HOME/shared/initial-admin-password" ] || fail "an admin was generated over restored users"
expect "API login with the restored admin" \
  "$("$MMCTL" compose exec -T api python -c "import base64, urllib.request as u
r = u.Request('http://127.0.0.1:8000/me', headers={'Authorization': 'Basic ' + base64.b64encode(b'admin:$ADMIN_PW').decode()})
print(u.urlopen(r, timeout=10).status)")" 200
if curl -sk -o /dev/null https://localhost/; then fail "port 443 answers with --no-tls"; fi
echo "ok: nothing on 443"

step "All release installer checks passed"
