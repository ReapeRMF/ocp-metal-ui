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
TLS_HOSTNAME="${TLS_HOSTNAME:-$(hostname -f 2>/dev/null || hostname)}"
if [ -n "$TLS_CERT_HOST_FILE" ] || [ -n "$TLS_KEY_HOST_FILE" ]; then
  if [ -z "$TLS_CERT_HOST_FILE" ] || [ -z "$TLS_KEY_HOST_FILE" ] \
    || [ ! -f "$TLS_CERT_HOST_FILE" ] || [ ! -f "$TLS_KEY_HOST_FILE" ]; then
    echo "❌ Error: Set TLS_CERT_HOST_FILE and TLS_KEY_HOST_FILE to existing files."
    exit 1
  fi
  TLS_CERT_HOST_FILE="$(cd "$(dirname "$TLS_CERT_HOST_FILE")" && pwd -P)/$(basename "$TLS_CERT_HOST_FILE")"
  TLS_KEY_HOST_FILE="$(cd "$(dirname "$TLS_KEY_HOST_FILE")" && pwd -P)/$(basename "$TLS_KEY_HOST_FILE")"
else
  TLS_CERT_DIR="${TLS_CERT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/ocp-metal-ui/tls}"
  if [[ ! "$TLS_HOSTNAME" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "❌ Error: TLS_HOSTNAME must be a DNS hostname containing only letters, numbers, dots, or hyphens." >&2
    exit 1
  fi
  TLS_CERT_HOST_FILE="$TLS_CERT_DIR/tls.crt"
  TLS_KEY_HOST_FILE="$TLS_CERT_DIR/tls.key"
  if [ -e "$TLS_CERT_DIR" ] && [ ! -d "$TLS_CERT_DIR" ]; then
    echo "❌ Error: TLS_CERT_DIR must be a directory: $TLS_CERT_DIR" >&2
    exit 1
  fi
  TLS_DIR_CREATED=false
  if [ ! -d "$TLS_CERT_DIR" ]; then
    mkdir -p "$TLS_CERT_DIR"
    TLS_DIR_CREATED=true
  fi
  TLS_CERT_DIR="$(cd "$TLS_CERT_DIR" && pwd -P)"
  TLS_CERT_HOST_FILE="$TLS_CERT_DIR/tls.crt"
  TLS_KEY_HOST_FILE="$TLS_CERT_DIR/tls.key"
  if [ "$TLS_DIR_CREATED" = true ]; then
    chmod 700 "$TLS_CERT_DIR"
  fi
  if [ -e "$TLS_CERT_HOST_FILE" ] || [ -e "$TLS_KEY_HOST_FILE" ]; then
    if [ ! -f "$TLS_CERT_HOST_FILE" ] || [ ! -f "$TLS_KEY_HOST_FILE" ]; then
      echo "❌ Error: Both TLS certificate files must exist in $TLS_CERT_DIR." >&2
      exit 1
    fi
  else
    if ! command -v openssl >/dev/null 2>&1; then
      echo "❌ Error: openssl is required to create the default HTTPS certificate." >&2
      exit 1
    fi
    openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes \
      -keyout "$TLS_KEY_HOST_FILE" \
      -out "$TLS_CERT_HOST_FILE" \
      -subj "/CN=$TLS_HOSTNAME" \
      -addext "subjectAltName=DNS:localhost,DNS:$TLS_HOSTNAME,IP:127.0.0.1" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" \
      -addext "extendedKeyUsage=serverAuth"
    chmod 600 "$TLS_KEY_HOST_FILE"
    echo "⚠️  Created a self-signed HTTPS certificate: $TLS_CERT_HOST_FILE"
    echo "   Trust this certificate on operator workstations or accept the browser warning."
  fi
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "❌ Error: openssl is required to validate the HTTPS certificate and key." >&2
  exit 1
fi
if ! openssl x509 -in "$TLS_CERT_HOST_FILE" -noout >/dev/null 2>&1; then
  echo "❌ Error: TLS certificate is not a readable PEM X.509 certificate: $TLS_CERT_HOST_FILE" >&2
  exit 1
fi
if ! openssl x509 -in "$TLS_CERT_HOST_FILE" -noout -checkend 0 >/dev/null 2>&1; then
  echo "❌ Error: TLS certificate is expired or cannot be parsed: $TLS_CERT_HOST_FILE" >&2
  exit 1
fi
if ! openssl x509 -in "$TLS_CERT_HOST_FILE" -noout -checkhost "$TLS_HOSTNAME" >/dev/null 2>&1; then
  echo "❌ Error: TLS certificate does not match TLS_HOSTNAME=$TLS_HOSTNAME." >&2
  echo "   Set TLS_HOSTNAME to a name in the certificate, or provide a matching certificate." >&2
  exit 1
fi
if ! openssl pkey -in "$TLS_KEY_HOST_FILE" -passin pass: -noout >/dev/null 2>&1; then
  echo "❌ Error: TLS private key must be a readable, unencrypted PEM key: $TLS_KEY_HOST_FILE" >&2
  exit 1
fi
CERT_PUBLIC_KEY="$(openssl x509 -in "$TLS_CERT_HOST_FILE" -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | sha256sum | cut -d ' ' -f1)"
KEY_PUBLIC_KEY="$(openssl pkey -in "$TLS_KEY_HOST_FILE" -passin pass: -pubout -outform DER \
  | sha256sum | cut -d ' ' -f1)"
if [ "$CERT_PUBLIC_KEY" != "$KEY_PUBLIC_KEY" ]; then
  echo "❌ Error: TLS certificate and private key do not match." >&2
  exit 1
fi

TLS_ARGS=(
  -v "$TLS_CERT_HOST_FILE:/tls/tls.crt:ro,Z"
  -v "$TLS_KEY_HOST_FILE:/tls/tls.key:ro,Z"
  -e TLS_CERT_FILE=/tls/tls.crt
  -e TLS_KEY_FILE=/tls/tls.key
)

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
echo "👉 Point your browser to: https://${TLS_HOSTNAME}:${HOST_PORT:-8080}"
if openssl x509 -in "$TLS_CERT_HOST_FILE" -noout -checkip 127.0.0.1 >/dev/null 2>&1; then
  echo "👉 On the container host, use: https://127.0.0.1:${HOST_PORT:-8080}"
fi
echo "   HTTPS is enabled. A self-signed certificate requires browser trust/acceptance."
echo "💾 SQLite persists in container volume: $DATA_VOLUME"
echo "📂 Ansible workspace is mounted from: $ANSIBLE_HOST_DIR"
echo "   Set ANSIBLE_HOST_DIR before deployment to use another host directory."
echo "   Ensure the container user (UID 10001) can write to that directory."
echo "⚠️  Restrict access to a trusted management network; the API has no authentication."
echo "========================================================="
