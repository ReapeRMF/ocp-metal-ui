#!/usr/bin/env bash
set -euo pipefail

# Remove a checkout vault.password only when it is the old staged copy
# (uid 10001, mode 0400) and it is not the configured password source.
checkout_vault_copy_should_be_removed() {
  local target="$1"
  local source="${2:-}"
  local target_real source_real uid mode
  if [ ! -f "$target" ]; then
    return 1
  fi
  if [ -n "$source" ] && [ -e "$source" ]; then
    target_real="$(readlink -f "$target")"
    source_real="$(readlink -f "$source")"
    if [ "$target_real" = "$source_real" ]; then
      return 1
    fi
  fi
  uid="$(stat -c '%u' "$target")"
  mode="$(stat -c '%a' "$target")"
  [ "$uid" = "10001" ] && [ "$mode" = "400" ]
}

if [ "${1:-}" = "--vault-cleanup-decision" ]; then
  if checkout_vault_copy_should_be_removed "$2" "${3:-}"; then
    exit 0
  fi
  exit 1
fi

echo "========================================================="
echo "🔴 Disconnected Phase: Deploying the airgap UI"
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
    san="DNS:localhost,DNS:${TLS_HOSTNAME},IP:127.0.0.1"
    if [[ "${BIND_ADDRESS:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
      && [ "$BIND_ADDRESS" != "0.0.0.0" ] && [ "$BIND_ADDRESS" != "127.0.0.1" ]; then
      san="${san},IP:${BIND_ADDRESS}"
    fi
    openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes \
      -keyout "$TLS_KEY_HOST_FILE" \
      -out "$TLS_CERT_HOST_FILE" \
      -subj "/CN=$TLS_HOSTNAME" \
      -addext "subjectAltName=${san}" \
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

# 1. Load the pre-baked application image directly into local storage.
# The connected build stores the tar under container_images/ and this
# script at the payload root. A tar beside the script still wins.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BUNDLE_FILE=""
for candidate in \
  "$SCRIPT_DIR/ocp-metal-ui-engine-bundle.tar" \
  "$SCRIPT_DIR/container_images/ocp-metal-ui-engine-bundle.tar" \
  "${PWD}/ocp-metal-ui-engine-bundle.tar"
do
  if [ -f "$candidate" ]; then
    BUNDLE_FILE="$candidate"
    break
  fi
done
if [ -z "$BUNDLE_FILE" ]; then
  echo "❌ Error: ocp-metal-ui-engine-bundle.tar was not found beside this script or in container_images/." >&2
  exit 1
fi
echo "⚙️ Ingesting pre-baked application image matrix..."
$ENGINE load -i "$BUNDLE_FILE"

# The UI starts before the edge play, so this script loads the same
# PostgreSQL image Quay will use and starts a second container. Quay
# keeps port 5432 and the quay database. This container publishes
# 127.0.0.1:5433 only.
start_ui_database() {
  local image_tar image data_dir init_dir credentials publish
  local db_host db_port ready state ui_env app_password admin_password
  image_tar="${POSTGRES_IMAGE_TAR:-/var/lib/edge-images/postgresql/postgresql.tar}"
  image="${POSTGRES_IMAGE:-registry.access.redhat.com/hi/postgresql:latest}"
  data_dir="${UI_DB_DIR:-/var/lib/ocp-metal-ui/postgres}"
  init_dir="${UI_DB_INIT_DIR:-/var/lib/ocp-metal-ui/db-init}"
  credentials="${UI_DB_CREDENTIALS:-/var/lib/ocp-metal-ui/db.env}"
  publish="${UI_DB_PUBLISH:-127.0.0.1:5433}"
  ui_env="$(dirname "$credentials")/ui.env"
  if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Error: start the UI database as root. $data_dir is owned by uid 999." >&2
    exit 1
  fi
  if ! $ENGINE image exists "$image"; then
    if ! $ENGINE image exists registry.access.redhat.com/hi/postgresql:latest; then
      if [ ! -s "$image_tar" ]; then
        echo "❌ Error: PostgreSQL image $image is not loaded and $image_tar is missing." >&2
        exit 1
      fi
      echo "⚙️ Loading PostgreSQL image from $image_tar"
      $ENGINE load -i "$image_tar"
    fi
    if $ENGINE image exists registry.access.redhat.com/hi/postgresql:latest \
      && [ "$image" != "registry.access.redhat.com/hi/postgresql:latest" ]; then
      $ENGINE tag registry.access.redhat.com/hi/postgresql:latest "$image"
    fi
  fi
  if ! $ENGINE image exists "$image"; then
    echo "❌ Error: PostgreSQL image $image is not available." >&2
    exit 1
  fi
  install -d -o root -g root -m 755 "$(dirname "$credentials")" "$init_dir"
  install -d -o 999 -g 999 -m 700 "$data_dir"
  if [ ! -f "$credentials" ]; then
    if [ -s "$data_dir/PG_VERSION" ]; then
      echo "❌ Error: the UI database is initialized but $credentials is missing." >&2
      exit 1
    fi
    app_password="$(openssl rand -hex 24)"
    admin_password="$(openssl rand -hex 24)"
    previous_umask="$(umask)"
    umask 077
    printf 'UI_DB_PASSWORD=%s\nPOSTGRES_PASSWORD=%s\n' "$app_password" "$admin_password" > "$credentials"
    chmod 600 "$credentials"
    umask "$previous_umask"
    unset app_password admin_password
  fi
  set -a
  # shellcheck disable=SC1090
  . "$credentials"
  set +a
  if [ -z "${UI_DB_PASSWORD:-}" ] || [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "❌ Error: $credentials must set UI_DB_PASSWORD and POSTGRES_PASSWORD." >&2
    exit 1
  fi
  cat > "$init_dir/01-ocp-metal-ui.sh" << 'EOF'
#!/bin/bash
set -euo pipefail
if [ -z "${UI_DB_PASSWORD:-}" ]; then
  echo "UI_DB_PASSWORD is required." >&2
  exit 1
fi
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname postgres << SQL
CREATE ROLE ocp_metal_ui LOGIN PASSWORD '${UI_DB_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE ocp_metal_ui OWNER ocp_metal_ui;
REVOKE ALL ON DATABASE ocp_metal_ui FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE ocp_metal_ui TO ocp_metal_ui;
SQL
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname ocp_metal_ui << 'SQL'
GRANT ALL ON SCHEMA public TO ocp_metal_ui;
SQL
EOF
  chmod 755 "$init_dir/01-ocp-metal-ui.sh"
  if $ENGINE container exists ocp-metal-ui-db; then
    state="$($ENGINE inspect -f '{{.State.Status}}' ocp-metal-ui-db)"
    if [ "$state" != "running" ] && [ ! -s "$data_dir/PG_VERSION" ]; then
      $ENGINE rm -f ocp-metal-ui-db >/dev/null
    fi
  fi
  if $ENGINE container exists ocp-metal-ui-db; then
    $ENGINE start ocp-metal-ui-db >/dev/null
  else
    $ENGINE run -d \
      --name ocp-metal-ui-db \
      --restart unless-stopped \
      -p "${publish}:5432" \
      -v "${data_dir}:/var/lib/postgresql/data:Z" \
      -v "${init_dir}:/docker-entrypoint-initdb.d:ro,Z" \
      --env-file "$credentials" \
      "$image"
  fi
  db_host="${publish%:*}"
  db_port="${publish##*:}"
  ready=0
  for _ in $(seq 1 90); do
    if $ENGINE exec -e PGPASSWORD="$UI_DB_PASSWORD" ocp-metal-ui-db \
      psql -h 127.0.0.1 -U ocp_metal_ui -d ocp_metal_ui -c 'SELECT 1' >/dev/null 2>&1; then
      ready=1
      break
    fi
    state="$($ENGINE inspect -f '{{.State.Status}}' ocp-metal-ui-db 2>/dev/null || true)"
    if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
      break
    fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    echo "❌ Error: ocp-metal-ui-db did not accept connections on ${publish}." >&2
    echo "   If this data directory is only partly initialized, remove ${data_dir} and start again." >&2
    echo "   Keep ${credentials}. It holds the passwords from the first attempt." >&2
    $ENGINE logs ocp-metal-ui-db >&2 || true
    exit 1
  fi
  previous_umask="$(umask)"
  umask 077
  printf 'DATABASE_URL=%s\n' \
    "postgresql://ocp_metal_ui:${UI_DB_PASSWORD}@${db_host}:${db_port}/ocp_metal_ui?sslmode=disable" \
    > "$ui_env"
  chmod 600 "$ui_env"
  umask "$previous_umask"
  unset UI_DB_PASSWORD POSTGRES_PASSWORD
  UI_DATABASE_ENV_FILE="$ui_env"
  UI_DB_DATA_DIR="$data_dir"
  UI_DB_CREDENTIALS_FILE="$credentials"
  UI_DB_PUBLISH_ADDRESS="$publish"
}

start_ui_database

# 2. Clean up any existing instances running on the port
echo "🛑 Tearing down old instances if present..."
$ENGINE rm -f ocp-metal-ui || true

ANSIBLE_HOST_DIR="${ANSIBLE_HOST_DIR:-$PWD/ansible-workspace}"
mkdir -p "$ANSIBLE_HOST_DIR"
ANSIBLE_HOST_DIR="$(cd "$ANSIBLE_HOST_DIR" && pwd -P)"
if [ "$ANSIBLE_HOST_DIR" = "/" ]; then
  echo "❌ Error: ANSIBLE_HOST_DIR must be a dedicated workspace, not /."
  exit 1
fi
case "$ANSIBLE_HOST_DIR" in
  /var/ocp-config|/var/ocp-config/*|/opt/ocp|/opt/ocp/*|/etc/ansible|/etc/ansible/*)
    echo "❌ Error: ANSIBLE_HOST_DIR is inside the configurator install ($ANSIBLE_HOST_DIR)." >&2
    echo "   The configurator keeps /var/ocp-config, /opt/ocp, and /etc/ansible." >&2
    echo "   Choose a separate workspace, for example /var/lib/ocp-metal-ui or ./ansible-workspace." >&2
    exit 1
    ;;
esac
chmod g+rwx,g+s "$ANSIBLE_HOST_DIR"
ANSIBLE_HOST_GID="$(stat -c '%g' "$ANSIBLE_HOST_DIR")"
if [ "$ENGINE" = "podman" ] && [ "$(id -u)" -ne 0 ]; then
  USER_NAMESPACE_ARGS=("--userns=keep-id:uid=10001,gid=10001")
  GROUP_ARGS=()
elif [ "$ENGINE" = "podman" ]; then
  # A root systemd service cannot use keep-id. Container create then
  # fails with OCI permission denied. The image runs as uid 10001.
  USER_NAMESPACE_ARGS=()
  GROUP_ARGS=()
  chown 10001:10001 "$ANSIBLE_HOST_DIR"
  chmod 0750 "$ANSIBLE_HOST_DIR"
  tls_dir=$(dirname "$TLS_CERT_HOST_FILE")
  chown 10001:10001 "$tls_dir" "$TLS_CERT_HOST_FILE" "$TLS_KEY_HOST_FILE"
  chmod 0750 "$tls_dir"
  chmod 0440 "$TLS_CERT_HOST_FILE" "$TLS_KEY_HOST_FILE"
else
  USER_NAMESPACE_ARGS=()
  GROUP_ARGS=(--group-add "$ANSIBLE_HOST_GID")
fi

VAULT_PASSWORD_HOST_FILE="${VAULT_PASSWORD_FILE:-/root/.vault_pass}"
VAULT_PASSWORD_SOURCE="$VAULT_PASSWORD_HOST_FILE"
VAULT_ARGS=()
VAULT_STAGE=""
remove_secret_file() {
  local target="$1"
  if [ ! -e "$target" ]; then
    return 0
  fi
  if command -v shred >/dev/null 2>&1; then
    shred -u -- "$target" 2>/dev/null || rm -f -- "$target"
  else
    rm -f -- "$target"
  fi
}
cleanup_vault_material() {
  if [ -n "${VAULT_STAGE:-}" ]; then
    remove_secret_file "$VAULT_STAGE"
    VAULT_STAGE=""
  fi
  # Only the old staged copy (uid 10001, mode 0400) is removed, and never
  # when that path is the configured vault password.
  if [ -n "${SCRIPT_DIR:-}" ] && checkout_vault_copy_should_be_removed \
    "$SCRIPT_DIR/vault.password" "${VAULT_PASSWORD_SOURCE:-}"; then
    remove_secret_file "$SCRIPT_DIR/vault.password"
  fi
}
trap cleanup_vault_material EXIT
if [ -f "$VAULT_PASSWORD_HOST_FILE" ]; then
  VAULT_PASSWORD_HOST_FILE="$(cd "$(dirname "$VAULT_PASSWORD_HOST_FILE")" && pwd -P)/$(basename "$VAULT_PASSWORD_HOST_FILE")"
  vault_mount_opts=ro
  if [ "$ENGINE" = "podman" ] && [ "$(id -u)" -eq 0 ]; then
    # The running container bind-mounts this file, so it stays under
    # /var/lib and is not removed when the script exits.
    install -d -o root -g root -m 755 /var/lib/ocp-metal-ui
    VAULT_STAGE="$(mktemp /var/lib/ocp-metal-ui/vault.password.XXXXXX)"
    install -o 10001 -g 10001 -m 0400 "$VAULT_PASSWORD_HOST_FILE" "$VAULT_STAGE"
    mv -f -- "$VAULT_STAGE" /var/lib/ocp-metal-ui/vault.password
    VAULT_STAGE=""
    chown 10001:10001 /var/lib/ocp-metal-ui/vault.password
    chmod 0400 /var/lib/ocp-metal-ui/vault.password
    VAULT_PASSWORD_HOST_FILE="/var/lib/ocp-metal-ui/vault.password"
    vault_mount_opts=ro,Z
  fi
  VAULT_ARGS=(
    -v "$VAULT_PASSWORD_HOST_FILE:/vault/password:${vault_mount_opts}"
    -e VAULT_PASSWORD_FILE=/vault/password
  )
else
  echo "⚠️  Vault password file not found at $VAULT_PASSWORD_HOST_FILE." >&2
  echo "   The pull secret built into the image stays locked until that file is mounted." >&2
fi

# 3. Start the application. The image still declares /data; the database
# is the PostgreSQL container, so that mount stays empty.
API_TOKEN_FILE="${API_TOKEN_FILE:-/var/lib/ocp-metal-ui/api.token}"
INSPECTION_TOKEN_FILE="${INSPECTION_TOKEN_FILE:-/var/lib/ocp-metal-ui/inspection.token}"
INSPECTION_CURL_CONFIG="${INSPECTION_CURL_CONFIG:-/var/lib/ocp-metal-ui/inspection.curl}"
install -d -o root -g root -m 755 "$(dirname "$API_TOKEN_FILE")" "$(dirname "$INSPECTION_TOKEN_FILE")"
write_private_token() {
  local path="$1"
  local previous_umask
  previous_umask="$(umask)"
  umask 077
  if [ ! -s "$path" ]; then
    openssl rand -hex 32 > "$path"
  fi
  chmod 600 "$path"
  umask "$previous_umask"
  if [ "$(id -u)" -eq 0 ]; then
    chown 10001:10001 "$path"
    chmod 0400 "$path"
  fi
}
write_private_token "$API_TOKEN_FILE"
write_private_token "$INSPECTION_TOKEN_FILE"
previous_umask="$(umask)"
umask 077
python3 - "$INSPECTION_TOKEN_FILE" "$INSPECTION_CURL_CONFIG" << 'PY'
import sys
from pathlib import Path
token = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
Path(sys.argv[2]).write_text(
    'header = "Authorization: Bearer %s"\n' % token,
    encoding="utf-8",
)
PY
chmod 600 "$INSPECTION_CURL_CONFIG"
umask "$previous_umask"
if [ "$(id -u)" -eq 0 ]; then
  chown 10001:10001 "$INSPECTION_CURL_CONFIG"
  chmod 0400 "$INSPECTION_CURL_CONFIG"
fi
install -d -o 10001 -g 10001 -m 755 "$(dirname "$UI_DATABASE_ENV_FILE")/empty"
echo "🚀 Starting the offline UI..."
$ENGINE run -d \
  "${USER_NAMESPACE_ARGS[@]}" \
  "${GROUP_ARGS[@]}" \
  --name ocp-metal-ui \
  --restart unless-stopped \
  --network host \
  --env-file "$UI_DATABASE_ENV_FILE" \
  -e "HOST=${BIND_ADDRESS:-127.0.0.1}" \
  -e "PORT=${HOST_PORT:-3003}" \
  -e "TLS_HOSTNAME=${TLS_HOSTNAME}" \
  -e API_TOKEN_FILE=/run/secrets/api-token \
  -e INSPECTION_TOKEN_FILE=/run/secrets/inspection-token \
  -v "${API_TOKEN_FILE}:/run/secrets/api-token:ro,Z" \
  -v "${INSPECTION_TOKEN_FILE}:/run/secrets/inspection-token:ro,Z" \
  -v "$(dirname "$UI_DATABASE_ENV_FILE")/empty:/data:Z" \
  -v "$ANSIBLE_HOST_DIR:/ansible:Z" \
  -e ANSIBLE_WORKING_DIR=/ansible \
  "${TLS_ARGS[@]}" \
  "${VAULT_ARGS[@]}" \
  localhost/ocp-metal-ui-engine:latest

echo "========================================================="
echo "✅ Operational Success! The wizard is live completely offline."
echo "👉 Point your browser to: https://${TLS_HOSTNAME}:${HOST_PORT:-3003}"
echo "👉 Listening on ${BIND_ADDRESS:-127.0.0.1}:${HOST_PORT:-3003}."
case "${BIND_ADDRESS:-127.0.0.1}" in
  127.0.0.1|0.0.0.0)
    if openssl x509 -in "$TLS_CERT_HOST_FILE" -noout -checkip 127.0.0.1 >/dev/null 2>&1; then
      echo "👉 On the container host, use: https://127.0.0.1:${HOST_PORT:-3003}"
    fi
    ;;
esac
echo "   HTTPS is enabled. A self-signed certificate requires browser trust/acceptance."
echo "💾 PostgreSQL data directory: ${UI_DB_DATA_DIR}"
echo "   Database ocp_metal_ui listens on ${UI_DB_PUBLISH_ADDRESS}."
echo "   Credentials: ${UI_DB_CREDENTIALS_FILE}"
echo "🔑 Admin API token file: ${API_TOKEN_FILE}"
echo "   Paste that token into the UI. It authorizes every /api/ route except health."
echo "🔑 Inspection token file: ${INSPECTION_TOKEN_FILE}"
echo "   The collector uses only this token. Curl config: ${INSPECTION_CURL_CONFIG}"
echo "   Both files stay outside the git checkout. Delete a file before the next deploy to rotate it."
echo "📂 Ansible workspace is mounted from: $ANSIBLE_HOST_DIR"
echo "   Set ANSIBLE_HOST_DIR before deployment to use another host directory."
echo "   Ensure the container user (UID 10001) can write to that directory."
echo "⚠️  The default bind is 127.0.0.1. Set BIND_ADDRESS only for a network you trust."
echo "   Every /api/ route except /api/v1/health requires the admin token."
echo "   POST /api/v1/inspections also accepts the inspection token and nothing else."
echo "========================================================="
