#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

IMAGE_NAME="localhost/ocp-metal-ui-engine:latest"
BUNDLE_FILE="$SCRIPT_DIR/ocp-metal-ui-engine-bundle.tar"
TEMP_BUNDLE=""

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

for required_file in package.json package-lock.json Containerfile.airgap server.py; do
  if [ ! -f "$required_file" ]; then
    echo "❌ Error: Required build input is missing: $required_file" >&2
    exit 1
  fi
done

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

# 2. Package the UI and SQLite API into a self-contained image.
echo "📦 Building $IMAGE_NAME..."
"$ENGINE" build -f Containerfile.airgap -t "$IMAGE_NAME" .

# 3. Export to a temporary file so a failed build never destroys a previous bundle.
TEMP_BUNDLE="$(mktemp "$BUNDLE_FILE.tmp.XXXXXX")"
echo "💾 Exporting the image archive..."
"$ENGINE" save -o "$TEMP_BUNDLE" "$IMAGE_NAME"
if [ ! -s "$TEMP_BUNDLE" ]; then
  echo "❌ Error: Container engine produced an empty image archive." >&2
  exit 1
fi
mv -f -- "$TEMP_BUNDLE" "$BUNDLE_FILE"
TEMP_BUNDLE=""

echo "========================================================="
echo "✅ Success! Transfer 'ocp-metal-ui-engine-bundle.tar' and"
echo "   'airgap-deploy-bundle.sh' across your data boundary."
echo "📦 Image: $IMAGE_NAME"
echo "📏 Archive size: $(du -h "$BUNDLE_FILE" | cut -f1)"
echo "========================================================="
