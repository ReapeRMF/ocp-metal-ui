#!/usr/bin/env bash
# A deploy run must make an existing pull-secret.json match the configured
# owner, group, and mode, and the directory match the directory mode.
# A second run changes nothing.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$ROOT/airgap-deploy-bundle.sh"
TMP="$(mktemp -d)"

if [ "$(id -u)" -eq 0 ]; then
  AS_ROOT=()
else
  AS_ROOT=(sudo)
fi

cleanup() {
  "${AS_ROOT[@]}" rm -rf "$TMP"
}
trap cleanup EXIT

fail() {
  echo "pull secret align failed: $1" >&2
  exit 1
}

as_root() {
  "${AS_ROOT[@]}" "$@"
}

mode_of() {
  as_root stat -c '%a' "$1"
}

owner_of() {
  as_root stat -c '%u:%g' "$1"
}

ctime_of() {
  as_root stat -c '%Z' "$1"
}

align() {
  as_root env \
    OCP_METAL_UI_PULL_SECRET_OWNER=10001 \
    OCP_METAL_UI_PULL_SECRET_GROUP=10001 \
    OCP_METAL_UI_PULL_SECRET_MODE=0640 \
    OCP_METAL_UI_PULL_SECRET_DIR_MODE=0750 \
    OCP_METAL_UI_PULL_SECRET_FILE_NAME=pull-secret.json \
    "$SCRIPT" --align-pull-secret "$1"
}

fresh="$TMP/fresh"
align "$fresh"
as_root test -d "$fresh" || fail "directory was not created"
as_root test ! -e "$fresh/pull-secret.json" || fail "align created a pull secret file"
[ "$(owner_of "$fresh")" = "10001:10001" ] || fail "new directory owner is $(owner_of "$fresh")"
[ "$(mode_of "$fresh")" = "750" ] || fail "new directory mode is $(mode_of "$fresh")"

dir="$TMP/existing"
file="$dir/pull-secret.json"
secret='{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
as_root mkdir -p "$dir"
as_root chmod 0755 "$dir"
printf '%s\n' "$secret" | as_root tee "$file" >/dev/null
as_root chown 0:0 "$dir" "$file"
as_root chmod 0600 "$file"

align "$dir"
[ "$(owner_of "$dir")" = "10001:10001" ] || fail "directory owner is $(owner_of "$dir")"
[ "$(mode_of "$dir")" = "750" ] || fail "directory mode is $(mode_of "$dir")"
[ "$(owner_of "$file")" = "10001:10001" ] || fail "file owner is $(owner_of "$file")"
[ "$(mode_of "$file")" = "640" ] || fail "file mode is $(mode_of "$file")"
[ "$(as_root cat "$file")" = "$secret" ] || fail "file contents changed"

sleep 1
dir_ctime="$(ctime_of "$dir")"
file_ctime="$(ctime_of "$file")"
align "$dir"
[ "$(ctime_of "$dir")" = "$dir_ctime" ] || fail "second run changed the directory"
[ "$(ctime_of "$file")" = "$file_ctime" ] || fail "second run changed the file"
[ "$(mode_of "$file")" = "640" ] || fail "second run changed the file mode"

as_root chmod 0644 "$file"
sleep 1
dir_ctime="$(ctime_of "$dir")"
align "$dir"
[ "$(owner_of "$file")" = "10001:10001" ] || fail "mode-only run changed the owner"
[ "$(mode_of "$file")" = "640" ] || fail "mode-only run left mode $(mode_of "$file")"
[ "$(ctime_of "$dir")" = "$dir_ctime" ] || fail "mode-only run changed the directory"

target="$TMP/outside"
printf 'keep\n' > "$target"
chmod 0600 "$target"
target_owner="$(stat -c '%u' "$target")"
linkdir="$TMP/linked"
as_root mkdir -p "$linkdir"
as_root chown 10001:10001 "$linkdir"
as_root chmod 0750 "$linkdir"
as_root ln -s "$target" "$linkdir/pull-secret.json"
if align "$linkdir" 2>/dev/null; then
  fail "symlink was accepted"
fi
[ "$(cat "$target")" = "keep" ] || fail "symlink target was rewritten"
[ "$(stat -c '%a' "$target")" = "600" ] || fail "symlink target mode changed"
[ "$(stat -c '%u' "$target")" = "$target_owner" ] || fail "symlink target owner changed"

echo "pull secret align: pass"
