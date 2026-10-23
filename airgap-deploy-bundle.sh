#!/usr/bin/env bash
set -euo pipefail

echo "========================================================="
echo "🔴 Disconnected Phase: Deploying ocp-metal-UI with SQLite"
echo "========================================================="

if command -v podman &> /dev/null; then ENGINE="podman";
elif command -v docker &> /dev/null; then ENGINE="docker";
else echo "❌ Error: Neither podman nor docker found on target host."; exit 1; fi

TLS_ARGS=()
TLS_CERT_HOST_FILE="${TLS_CERT_HOST_FILE:-}"
TLS_KEY_HOST_FILE="${TLS_KEY_HOST_FILE:-}"
if [ -n "$TLS_CERT_HOST_FILE" ] || [ -n "$TLS_KEY_HOST_FILE" ]; then
  if [ -z "$TLS_CERT_HOST_FILE" ] || [ -z "$TLS_KEY_HOST_FILE" ] \
    || [ ! -f "$TLS_CERT_HOST_FILE" ] || [ ! -f "$TLS_KEY_HOST_FILE" ]; then
    echo "❌ Error: Set TLS_CERT_HOST_FILE and TLS_KEY_HOST_FILE to existing files."
    exit 1
  fi
  TLS_CERT_HOST_FILE="$(cd "$(dirname "$TLS_CERT_HOST_FILE")" && pwd -P)/$(basename "$TLS_CERT_HOST_FILE")"
  TLS_KEY_HOST_FILE="$(cd "$(dirname "$TLS_KEY_HOST_FILE")" && pwd -P)/$(basename "$TLS_KEY_HOST_FILE")"
  TLS_ARGS=(
    -v "$TLS_CERT_HOST_FILE:/tls/tls.crt:ro,Z"
    -v "$TLS_KEY_HOST_FILE:/tls/tls.key:ro,Z"
    -e TLS_CERT_FILE=/tls/tls.crt
    -e TLS_KEY_FILE=/tls/tls.key
  )
fi

# 1. Load the pre-baked application image directly into local storage
echo "⚙️ Ingesting pre-baked application image matrix..."
$ENGINE load -i ocp-metal-ui-engine-bundle.tar

# 2. Clean up any existing instances running on the port
echo "🛑 Tearing down old instances if present..."
$ENGINE rm -f ocp-metal-ui || true

# 3. Keep SQLite data outside the replaceable application container
DATA_VOLUME="ocp-metal-ui-data"
$ENGINE volume create "$DATA_VOLUME" >/dev/null

ANSIBLE_HOST_DIR="${ANSIBLE_HOST_DIR:-$PWD/ansible-workspace}"
mkdir -p "$ANSIBLE_HOST_DIR"
ANSIBLE_HOST_DIR="$(cd "$ANSIBLE_HOST_DIR" && pwd -P)"
if [ "$ANSIBLE_HOST_DIR" = "/" ]; then
  echo "❌ Error: ANSIBLE_HOST_DIR must be a dedicated workspace, not /."
  exit 1
fi
chmod g+rwx,g+s "$ANSIBLE_HOST_DIR"
ANSIBLE_HOST_GID="$(stat -c '%g' "$ANSIBLE_HOST_DIR")"
if [ "$ENGINE" = "podman" ]; then
  USER_NAMESPACE_ARGS=(--userns=keep-id:uid=10001,gid=10001)
  GROUP_ARGS=()
else
  USER_NAMESPACE_ARGS=()
  GROUP_ARGS=(--group-add "$ANSIBLE_HOST_GID")
fi

# 4. Start the application with persistent database and Ansible workspace
echo "🚀 Starting the offline UI and SQLite API..."
$ENGINE run -d \
  "${USER_NAMESPACE_ARGS[@]}" \
  "${GROUP_ARGS[@]}" \
  --name ocp-metal-ui \
  --restart unless-stopped \
  -p "${HOST_PORT:-8080}:8080" \
  -v "$DATA_VOLUME:/data:Z" \
  -v "$ANSIBLE_HOST_DIR:/ansible:Z" \
  -e ANSIBLE_WORKING_DIR=/ansible \
  "${TLS_ARGS[@]}" \
  localhost/ocp-metal-ui-engine:latest

echo "========================================================="
echo "✅ Operational Success! The wizard is live completely offline."
if [ -n "$TLS_CERT_HOST_FILE" ]; then
  echo "👉 Point your browser to: https://localhost:${HOST_PORT:-8080}"
else
  echo "👉 Point your browser to: http://localhost:${HOST_PORT:-8080}"
  echo "⚠️  Redfish retrieval remains disabled until HTTPS is configured."
fi
echo "💾 SQLite persists in container volume: $DATA_VOLUME"
echo "📂 Ansible workspace is mounted from: $ANSIBLE_HOST_DIR"
echo "   Set ANSIBLE_HOST_DIR before deployment to use another host directory."
echo "   Ensure the container user (UID 10001) can write to that directory."
echo "⚠️  Restrict access to a trusted management network; the API has no authentication."
echo "========================================================="
