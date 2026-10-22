#!/usr/bin/env bash
set -euo pipefail

echo "========================================================="
echo "🔴 Connected Phase: Packaging ocp-metal-ui-engine"
echo "========================================================="

# 1. Clean old states
rm -f ocp-metal-ui-engine-bundle.tar

# 2. Check for container engine
if command -v podman &> /dev/null; then ENGINE="podman";
elif command -v docker &> /dev/null; then ENGINE="docker";
else echo "❌ Error: Neither podman nor docker found."; exit 1; fi

# 3. Compile React / PatternFly code into local static assets
echo "⚙️ Compiling production web bundles locally..."
npm run build

# 4. Bake compiled assets into local UBI Nginx base layer
echo "📦 Bundling assets into localhost/ocp-metal-ui-engine..."
$ENGINE build -f Containerfile.airgap -t localhost/ocp-metal-ui-engine:latest .

# 5. Export the entire finished web server image as a single air-gap tarball
echo "💾 Exporting final container image package..."
$ENGINE save -o ocp-metal-ui-engine-bundle.tar localhost/ocp-metal-ui-engine:latest

echo "========================================================="
echo "✅ Success! Transfer 'ocp-metal-ui-engine-bundle.tar' and"
echo "   'deploy-airgap.sh' across your data boundary."
echo "========================================================="

