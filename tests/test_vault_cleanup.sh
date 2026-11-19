#!/usr/bin/env bash
# The deploy script must not delete a real vault password that an operator
# kept at vault.password in the checkout. It may delete only the old staged
# copy: uid 10001, mode 0400, and not the configured source.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$ROOT/airgap-deploy-bundle.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ "$(id -u)" -eq 0 ]; then
  AS_ROOT=()
else
  AS_ROOT=(sudo)
fi

decide() {
  if "$SCRIPT" --vault-cleanup-decision "$1" "${2:-}"; then
    echo remove
  else
    echo keep
  fi
}

operator="$TMP/operator-vault.password"
printf 'real-password\n' > "$operator"
chmod 600 "$operator"

staged="$TMP/staged-vault.password"
printf 'staged-copy\n' > "$staged"
"${AS_ROOT[@]}" chown 10001:10001 "$staged"
"${AS_ROOT[@]}" chmod 0400 "$staged"

other="$TMP/other-vault.password"
printf 'other-password\n' > "$other"
chmod 600 "$other"

root_owned="$TMP/root-mode-vault.password"
printf 'root-copy\n' > "$root_owned"
"${AS_ROOT[@]}" chown 0:0 "$root_owned"
"${AS_ROOT[@]}" chmod 0400 "$root_owned"

fail() {
  echo "vault cleanup decision failed: $1" >&2
  exit 1
}

[ "$(decide "$operator" "$operator")" = "keep" ] || fail "configured source was removed"
[ "$(decide "$staged" "$operator")" = "remove" ] || fail "old staged copy was kept"
[ "$(decide "$staged" "$staged")" = "keep" ] || fail "staged file used as the source was removed"
[ "$(decide "$other" "$operator")" = "keep" ] || fail "operator-owned file was removed"
[ "$(decide "$root_owned" "$operator")" = "keep" ] || fail "root-owned mode 0400 file was removed"
[ "$(decide "$TMP/missing" "$operator")" = "keep" ] || fail "missing file was treated as removable"

echo "vault cleanup decisions: pass"
