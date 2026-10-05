#!/bin/sh
# Start the opt-in evidence object store (SeaweedFS, Apache-2.0; compose profile "evidence").
#
# Usage: objectstore-start.sh <config-path> <command> [args...]
#
# Renders the S3 identities file to <config-path> from the environment, then execs <command>.
# Two identities, nothing anonymous:
#   admin  MM_OBJECTSTORE_ADMIN_KEY / MM_OBJECTSTORE_ADMIN_SECRET  full access, for operators
#   edge   MM_EDGE_S3_ACCESS_KEY / MM_EDGE_S3_SECRET_KEY            "Write:<bucket>" only: it can put
#          (and delete) objects in the evidence bucket, but cannot read, list or create buckets
# Fails closed: a missing, short or unsafe value stops the container instead of starting an open
# store. Values are written into JSON, so they are restricted to a safe character set rather than
# escaped.
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: objectstore-start.sh <config-path> <command> [args...]" >&2
  exit 2
fi
config="$1"
shift

fail() {
  echo "objectstore: $*" >&2
  exit 1
}

# check NAME VALUE MIN_LEN
check() {
  case "$2" in
    "") fail "$1 must be set (see .env.example)" ;;
    *[!A-Za-z0-9._~+/=-]*) fail "$1 may only use the characters A-Z a-z 0-9 . _ ~ + / = -" ;;
  esac
  [ "${#2}" -ge "$3" ] || fail "$1 must be at least $3 characters"
  [ "${#2}" -le 128 ] || fail "$1 must be at most 128 characters"
}

admin_key="${MM_OBJECTSTORE_ADMIN_KEY:-}"
admin_secret="${MM_OBJECTSTORE_ADMIN_SECRET:-}"
edge_key="${MM_EDGE_S3_ACCESS_KEY:-}"
edge_secret="${MM_EDGE_S3_SECRET_KEY:-}"
bucket="${MM_EDGE_S3_BUCKET:-mine-evidence}"

check MM_OBJECTSTORE_ADMIN_KEY "$admin_key" 3
check MM_OBJECTSTORE_ADMIN_SECRET "$admin_secret" 16
check MM_EDGE_S3_ACCESS_KEY "$edge_key" 3
check MM_EDGE_S3_SECRET_KEY "$edge_secret" 16
[ "$admin_key" != "$edge_key" ] || fail "MM_EDGE_S3_ACCESS_KEY must differ from MM_OBJECTSTORE_ADMIN_KEY"
# S3 bucket naming: 3-63 of lowercase letters, digits, '.' and '-', starting and ending alphanumeric.
case "$bucket" in
  *[!a-z0-9.-]* | [.-]* | *[.-]) fail "MM_EDGE_S3_BUCKET is not a valid bucket name: $bucket" ;;
esac
[ "${#bucket}" -ge 3 ] && [ "${#bucket}" -le 63 ] || fail "MM_EDGE_S3_BUCKET must be 3-63 characters"

umask 077
tmp="$config.tmp.$$"
cat >"$tmp" <<EOF
{
  "identities": [
    {
      "name": "admin",
      "credentials": [{"accessKey": "$admin_key", "secretKey": "$admin_secret"}],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    },
    {
      "name": "edge",
      "credentials": [{"accessKey": "$edge_key", "secretKey": "$edge_secret"}],
      "actions": ["Write:$bucket"]
    }
  ]
}
EOF
mv "$tmp" "$config"
# The image's entrypoint drops to the unprivileged "seaweed" user, which must be able to read it.
if [ "$(id -u)" = "0" ] && id seaweed >/dev/null 2>&1; then
  chown seaweed:seaweed "$config"
fi

exec "$@"
