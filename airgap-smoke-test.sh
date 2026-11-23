#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://127.0.0.1:3003}"
TLS_CA_FILE="${TLS_CA_FILE:-}"
CHROME_BIN="${CHROME_BIN:-google-chrome}"
BROWSER_TIMEOUT="${BROWSER_TIMEOUT:-30}"

case "$BASE_URL" in
  https://*) ;;
  *)
    echo "Error: BASE_URL must use HTTPS." >&2
    exit 1
    ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required." >&2
  exit 1
fi
if ! command -v "$CHROME_BIN" >/dev/null 2>&1; then
  echo "Error: Chrome/Chromium was not found: $CHROME_BIN" >&2
  echo "Set CHROME_BIN to the browser executable." >&2
  exit 1
fi
if [ -n "$TLS_CA_FILE" ] && [ ! -f "$TLS_CA_FILE" ]; then
  echo "Error: TLS_CA_FILE does not exist: $TLS_CA_FILE" >&2
  exit 1
fi
if [ -n "$TLS_CA_FILE" ] && ! command -v certutil >/dev/null 2>&1; then
  echo "Error: certutil is required to pin TLS_CA_FILE for headless Chrome." >&2
  echo "Install the NSS tools package, or configure the certificate in Chrome's trust store." >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

export BASE_URL TLS_CA_FILE TEMP_DIR
mkdir -p "$TEMP_DIR/chrome-profile"
if [ -n "$TLS_CA_FILE" ]; then
  certutil -N -d "sql:$TEMP_DIR/chrome-profile" --empty-password
  certutil -A \
    -d "sql:$TEMP_DIR/chrome-profile" \
    -n "airgap-ui-smoke-test-peer" \
    -t "P,," \
    -i "$TLS_CA_FILE"
fi
python3 - <<'PY'
import json
import os
import re
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen


base_url = os.environ["BASE_URL"].rstrip("/") + "/"
ca_file = os.environ.get("TLS_CA_FILE")
context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def admin_token():
    value = os.environ.get("API_TOKEN", "").strip()
    if value:
        return value
    path = os.environ.get("API_TOKEN_FILE", "/var/lib/ocp-metal-ui/api.token")
    token_file = os.path.expanduser(path)
    if os.path.isfile(token_file):
        with open(token_file, encoding="utf-8") as handle:
            return handle.read().strip()
    raise RuntimeError("Set API_TOKEN or API_TOKEN_FILE. The token is not read from the command line.")


TOKEN = admin_token()


def get(path):
    request = Request(
        urljoin(base_url, path.lstrip("/")),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urlopen(request, context=context, timeout=10) as response:
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return body, response.headers.get_content_type()


health_body, _ = get("/api/v1/health")
if json.loads(health_body).get("status") != "ok":
    raise RuntimeError("Health API did not report status=ok.")

config_body, _ = get("/api/v1/config")
if json.loads(config_body).get("httpsEnabled") is not True:
    raise RuntimeError("The app reports that HTTPS is disabled.")

nodes_body, _ = get("/api/v1/nodes")
nodes = json.loads(nodes_body)
if not isinstance(nodes, list):
    raise RuntimeError("Inventory API did not return a node list.")
serial_numbers = [
    node["serial_number"]
    for node in nodes
    if isinstance(node, dict)
    and isinstance(node.get("serial_number"), str)
    and node["serial_number"]
]
with open(os.path.join(os.environ["TEMP_DIR"], "serial-numbers.txt"), "w", encoding="utf-8") as output:
    output.write("\n".join(serial_numbers))

html_body, content_type = get("/")
if content_type != "text/html":
    raise RuntimeError(f"Homepage returned unexpected content type: {content_type}")


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        source = attrs.get("src") if tag == "script" else attrs.get("href")
        if source and re.search(r"\.(?:js|css)(?:\?|$)", source):
            self.assets.append(source)


parser = AssetParser()
parser.feed(html_body.decode("utf-8"))
if not any(asset.split("?", 1)[0].endswith(".js") for asset in parser.assets):
    raise RuntimeError("Homepage did not reference a JavaScript bundle.")
if not any(asset.split("?", 1)[0].endswith(".css") for asset in parser.assets):
    raise RuntimeError("Homepage did not reference a stylesheet.")
for asset in parser.assets:
    body, _ = get(asset)
    if not body:
        raise RuntimeError(f"Frontend asset was empty: {asset}")

print(f"HTTPS API, homepage, and {len(parser.assets)} frontend assets: PASS")
print(f"Inventory API returned {len(nodes)} node(s).")

secret = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
save_request = Request(
    urljoin(base_url, "api/v1/install-secrets/pull-secret"),
    data=json.dumps({"pullSecret": secret}).encode(),
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urlopen(save_request, context=context, timeout=10) as response:
    saved = json.loads(response.read().decode())
    if response.status != 200 or saved.get("saved") is not True:
        raise RuntimeError("Pull secret save did not succeed.")
    if "dGVzdA==" in json.dumps(saved):
        raise RuntimeError("Pull secret save echoed the secret.")
status_request = Request(
    urljoin(base_url, "api/v1/install-secrets?outputDirectory=/ansible"),
    headers={"Authorization": f"Bearer {TOKEN}"},
)
with urlopen(status_request, context=context, timeout=10) as response:
    status_body = response.read().decode()
    status = json.loads(status_body)
    if not status.get("pullSecretReady"):
        raise RuntimeError(status.get("pullSecretError") or "Pull secret was not readable after save.")
    if "dGVzdA==" in status_body:
        raise RuntimeError("Pull secret status echoed the secret.")
inspection = {
    "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
    "bmc_address": "192.0.2.21",
    "inventory": {
        "system_vendor": {"serial_number": "SMOKE-SERIAL"},
        "cpu": {"count": 1, "model_name": "Example CPU"},
        "interfaces": [{"name": "eno5", "mac_address": "52:54:00:11:00:05"}],
        "disks": [{"name": "/dev/nvme0n1", "serial": "NVME-C1", "size": 1000}],
    },
}
inspect_request = Request(
    urljoin(base_url, "api/v1/inspections"),
    data=json.dumps(inspection).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(inspect_request, context=context, timeout=10) as response:
    if response.status != 202:
        raise RuntimeError(f"Inspection intake returned HTTP {response.status}")
generate = {
    "outputDirectory": "/ansible",
    "preferences": {
        inspection["uuid"]: {
            "ansibleHost": "control-01",
            "role": "master",
            "rendezvous": True,
            "ipv4LastOctet": "11",
            "rootDisk": "NVME-C1",
            "netType": "single",
            "singleNic": "eno5",
            "singleVlanEnabled": False,
        }
    },
    "blueprint": {
        "clusterName": "ocp-baremetal",
        "baseDomain": "ocp.internal",
        "controlPlaneTopology": "SingleNode",
        "machineNetworkCidr": "192.0.2.0/24",
        "networkGateway": "192.0.2.1",
    },
}
generate_request = Request(
    urljoin(base_url, "api/v1/ansible-files"),
    data=json.dumps(generate).encode(),
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urlopen(generate_request, context=context, timeout=20) as response:
    generated = json.loads(response.read().decode())
    if response.status != 200:
        raise RuntimeError(f"Generate returned HTTP {response.status}")
    if "dGVzdA==" in generated.get("installConfig", ""):
        raise RuntimeError("Generate echoed the pull secret in installConfig.")
print("Pull secret save, read, and generate: PASS")
PY

if ! timeout "$BROWSER_TIMEOUT" "$CHROME_BIN" \
  --headless \
  --disable-gpu \
  --disable-dev-shm-usage \
  --no-first-run \
  --user-data-dir="$TEMP_DIR/chrome-profile" \
  --virtual-time-budget=5000 \
  --dump-dom "$BASE_URL/" \
  >"$TEMP_DIR/page.html" 2>"$TEMP_DIR/chrome.log"; then
  echo "Error: Headless Chrome failed to load the UI within ${BROWSER_TIMEOUT}s." >&2
  cat "$TEMP_DIR/chrome.log" >&2
  exit 1
fi

if ! grep -q "Start cluster configuration" "$TEMP_DIR/page.html"; then
  echo "Error: The Builder landing page did not render its expected action." >&2
  cat "$TEMP_DIR/chrome.log" >&2
  exit 1
fi
if ! grep -q "OpenShift Bare-Metal Builder" "$TEMP_DIR/page.html"; then
  echo "Error: The Builder page did not render its expected title." >&2
  cat "$TEMP_DIR/chrome.log" >&2
  exit 1
fi

echo "Headless Chrome rendered the Builder landing page with normal TLS verification: PASS"

if [ -s "$TEMP_DIR/serial-numbers.txt" ]; then
  if ! timeout "$BROWSER_TIMEOUT" "$CHROME_BIN" \
    --headless \
    --disable-gpu \
    --disable-dev-shm-usage \
    --no-first-run \
    --user-data-dir="$TEMP_DIR/chrome-profile" \
    --virtual-time-budget=5000 \
    --dump-dom "${BASE_URL%/}/#/pxe/discovered" \
    >"$TEMP_DIR/discovered.html" 2>"$TEMP_DIR/chrome-discovered.log"; then
    echo "Error: Headless Chrome failed to load the discovered-node page." >&2
    cat "$TEMP_DIR/chrome-discovered.log" >&2
    exit 1
  fi
  python3 - <<'PY'
import os

with open(os.path.join(os.environ["TEMP_DIR"], "discovered.html"), encoding="utf-8") as page:
    html = page.read()
with open(os.path.join(os.environ["TEMP_DIR"], "serial-numbers.txt"), encoding="utf-8") as source:
    serial_numbers = [line for line in source.read().splitlines() if line]
if "Discovered Nodes" not in html:
    raise SystemExit("Error: The Discovered Nodes page did not render.")
missing = [serial for serial in serial_numbers if serial not in html]
if missing:
    raise SystemExit(f"Error: Discovered inventory did not render serials: {', '.join(missing)}")
print(f"Headless Chrome rendered all {len(serial_numbers)} discovered node(s): PASS")
PY
fi
