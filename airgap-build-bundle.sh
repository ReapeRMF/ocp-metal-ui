#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

IMAGE_NAME="localhost/ocp-metal-ui-engine:latest"
BUNDLE_FILE="$SCRIPT_DIR/ocp-metal-ui-engine-bundle.tar"
TEMP_BUNDLE=""
OUTPUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --output)
      OUTPUT="${2:-}"
      if [ -z "$OUTPUT" ]; then
        echo "usage: $0 [--output TAR]" >&2
        exit 1
      fi
      shift 2
      ;;
    *)
      echo "usage: $0 [--output TAR]" >&2
      exit 1
      ;;
  esac
done

if [ -n "$OUTPUT" ]; then
  case "$OUTPUT" in
    */)
      mkdir -p "$OUTPUT"
      BUNDLE_FILE="${OUTPUT}ocp-metal-ui-engine-bundle.tar"
      ;;
    *)
      if [ -d "$OUTPUT" ]; then
        BUNDLE_FILE="$OUTPUT/ocp-metal-ui-engine-bundle.tar"
      else
        mkdir -p "$(dirname "$OUTPUT")"
        BUNDLE_FILE="$OUTPUT"
      fi
      ;;
  esac
fi

cleanup() {
  if [ -n "$TEMP_BUNDLE" ]; then
    rm -f -- "$TEMP_BUNDLE"
  fi
}
trap cleanup EXIT

echo "========================================================="
echo "🔴 Connected Phase: Packaging ocp-metal-ui"
echo "========================================================="

if ! command -v npm >/dev/null 2>&1; then
  echo "❌ Error: npm is required to build the UI." >&2
  exit 1
fi

for required_file in package.json package-lock.json Containerfile.airgap server.py agent_install.py pull-secret.json; do
  if [ ! -f "$required_file" ]; then
    echo "❌ Error: Required build input is missing: $required_file" >&2
    if [ "$required_file" = "pull-secret.json" ]; then
      echo "   Place the cluster pull secret at pull-secret.json before building." >&2
      echo "   The build stores it in an Ansible vault. The plaintext file is not copied into the image." >&2
    fi
    exit 1
  fi
done

if [ -z "${VAULT_PASSWORD_FILE:-}" ] || [ ! -f "$VAULT_PASSWORD_FILE" ]; then
  echo "❌ Error: Set VAULT_PASSWORD_FILE to the Ansible vault password file." >&2
  echo "   The password is not copied into the image. Mount that file when the container starts." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ Error: python3 is required to vault pull-secret.json." >&2
  exit 1
fi

export VAULT_PASSWORD_FILE
python3 - << 'PY'
import json
import os
import sys
from pathlib import Path

import server

password_path = Path(os.environ["VAULT_PASSWORD_FILE"])
password = password_path.read_bytes().strip()
if not password:
    sys.exit("The vault password file is empty.")
text = Path("pull-secret.json").read_text(encoding="utf-8")
if any(marker in text for marker in ("a8c2744b", "e9bd34f4")):
    sys.exit("That pull secret is not accepted.")
try:
    normalized = server.normalize_pull_secret(text)
except ValueError as exc:
    sys.exit(str(exc))
parsed = json.loads(normalized)
if "cloud.openshift.com" not in parsed["auths"]:
    sys.exit(
        "pull-secret.json must include cloud.openshift.com. "
        "The mirror pull user is not the cluster pull secret."
    )
vault_text = server.format_pull_secret_vault(normalized, password)
destination = Path("pull-secret.vault")
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, vault_text.encode("utf-8"))
finally:
    os.close(fd)
os.chmod(destination, 0o600)
if normalized in vault_text:
    sys.exit("Refusing to write a vault file that still contains the pull secret.")
print(f"Pull secret vaulted ({len(parsed['auths'])} registries).")
PY
chmod 600 pull-secret.json pull-secret.vault

if command -v podman >/dev/null 2>&1; then
  ENGINE="$(command -v podman)"
elif command -v docker >/dev/null 2>&1; then
  ENGINE="$(command -v docker)"
else
  echo "❌ Error: Neither podman nor docker found on the build host." >&2
  exit 1
fi

# 1. Compile React / PatternFly code into local static assets.
echo "⚙️ Compiling the UI..."
npm run build

# 2. Package the UI and its API into a self-contained image.
echo "📦 Building $IMAGE_NAME..."
"$ENGINE" build -f Containerfile.airgap -t "$IMAGE_NAME" .

# 3. Export to a temporary file so a failed build never destroys a previous bundle.
TEMP_BUNDLE="$(mktemp "$(dirname "$BUNDLE_FILE")/$(basename "$BUNDLE_FILE").tmp.XXXXXX")"
echo "💾 Exporting the image archive..."
"$ENGINE" save -o "$TEMP_BUNDLE" "$IMAGE_NAME"
if [ ! -s "$TEMP_BUNDLE" ]; then
  echo "❌ Error: Container engine produced an empty image archive." >&2
  exit 1
fi
mv -f -- "$TEMP_BUNDLE" "$BUNDLE_FILE"
TEMP_BUNDLE=""

echo "========================================================="
echo "✅ Success! Transfer the image archive and"
echo "   'airgap-deploy-bundle.sh' across your data boundary."
echo "📦 Image: $IMAGE_NAME"
echo "📦 Archive: $BUNDLE_FILE"
echo "📏 Archive size: $(du -h "$BUNDLE_FILE" | cut -f1)"
echo "========================================================="
