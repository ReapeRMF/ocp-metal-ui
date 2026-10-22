#!/usr/bin/env bash
set -euo pipefail

echo "========================================================="
echo "🔴 Disconnected Phase: Deploying ocp-metal-UI-Engine"
echo "========================================================="

if command -v podman &> /dev/null; then ENGINE="podman";
elif command -v docker &> /dev/null; then ENGINE="docker";
else echo "❌ Error: Neither podman nor docker found on target host."; exit 1; fi

# 1. Load the pre-baked application image directly into local storage
echo "⚙️ Ingesting pre-baked application image matrix..."
$ENGINE load -i ocp-metal-ui-engine-bundle.tar

# 2. Clean up any existing instances running on the port
echo "🛑 Tearing down old instances if present..."
$ENGINE rm -f ocp-metal-ui || true

# 3. Fire up the hardened unprivileged isolation engine container
echo "🚀 Spawning secure offline server engine instance..."
$ENGINE run -d --name ocp-metal-ui -p 8080:8080 localhost/ocp-metal-ui-engine:latest

echo "========================================================="
echo "✅ Operational Success! The wizard is live completely offline."
echo "👉 Point your local web browser to: http://localhost:8080"
echo "========================================================="

