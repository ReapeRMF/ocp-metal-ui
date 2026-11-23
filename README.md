# OpenShift Bare-Metal Builder

Author: David Stratford, assisted by AI.

One container serves the PatternFly UI and its API. The operator reviews discovered hardware, sets disk and network choices, and writes Ansible files to a mounted workspace. The UI reads blueprint defaults, named hosts, and discovered hardware from PostgreSQL. It does not ship sample inventory. The node serial number is the hardware identifier that links a discovered system to its Ansible host.

## Ports

| Where | Address | Port |
| --- | --- | --- |
| Example installed host (`genesisIp`) | `192.0.2.35` | `3003` |
| This workstation | `127.0.0.1` | `18080` |

Port 3003 is the UI on the installed host. Semaphore on that host uses port 3000. The hardware collector on that host uses port 5000 and posts inspection reports to `https://127.0.0.1:3003` with no token. Boot files are served by httpd on port 80. See [Inspection client contract](#inspection-client-contract).

The workstation container is a separate process. Open it at `https://edge.example.com:18080` when the certificate was issued for that name.

`./airgap-deploy-bundle.sh` listens on `BIND_ADDRESS` (default `127.0.0.1`) and port `3003`. Set `BIND_ADDRESS` to publish another address, for example `192.0.2.35`. For the workstation, change only the port:

```sh
HOST_PORT=18080 ./airgap-deploy-bundle.sh
```

## Data flow

1. An inspection client sends a node UUID and `inventory` JSON to `POST /api/v1/inspections`, or to `POST /api/v1/inspections/{uuid}`.
2. The API creates a node from the first report for a UUID. A later report refreshes that node's BMC address, serial number, and inventory, and is stored as another revision. A blank or missing serial number or BMC address keeps the stored value. A non-blank value overwrites it. Preferences and other operator settings stay as they are.
3. The UI polls `GET /api/v1/nodes` every 15 seconds.
4. The operator can create named placeholder hosts before reports arrive, then link each name to a discovered node. The inventory group is set in application configuration.
5. **Review and confirm** lists discovered nodes and named placeholders. The operator can refresh before confirming.
6. Blueprint and Inventory Config can each save the current configuration to PostgreSQL and continue. **Generate Ansible Files** on Review writes the inventory and variables. It does not run Ansible.
7. PXE inspection and BMC retrieval are separate. If PXE or IPA is unavailable, the operator starts a BMC retrieval from the UI, enters the credentials for that request, and reads inventory from HPE iLO, Dell iDRAC, Supermicro, or another Redfish BMC. Both methods update the same node when the system UUID matches, and the last report wins.

The home page links to the cluster blueprint and to BMC profiles. Blueprint, inventory, and review are separate URLs. Browser Back and Forward move among those pages.

## Inspection reports

The body matches an Ironic inspection report: a JSON object with an `inventory` object and a node `uuid`. The UUID may be in the URL instead. `bmc_address` is optional. On a later report, a blank or missing BMC address or serial number keeps the stored value, and a non-blank one overwrites it.

The serial number is read from, in order:

- `inventory.system_vendor.serial_number`
- top-level `serial_number`
- `inventory.serial_number`
- `inventory.system.serial_number`

A discovered node needs a serial number before it can be written to the Ansible inventory. A named placeholder can be written before its hardware report arrives.

```json
{
  "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
  "bmc_address": "192.0.2.21",
  "inventory": {
    "system_vendor": { "serial_number": "NODE-SERIAL-01" },
    "cpu": { "count": 64, "model_name": "Example CPU" },
    "memory": { "physical_mb": 262144 },
    "disks": [
      { "name": "/dev/nvme0n1", "serial": "EXAMPLE123", "model": "Example NVMe", "size": 1000000000000 }
    ],
    "interfaces": [
      { "name": "eno5", "mac_address": "52:54:00:fa:19:bc" }
    ]
  }
}
```

A top-level serial is also accepted:

```json
{
  "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
  "serial_number": "NODE-SERIAL-01",
  "inventory": {
    "cpu": { "count": 64, "model_name": "Example CPU" }
  }
}
```

On the installed host, post the report to port 3003. The DNS name in the URL must match `TLS_HOSTNAME`. Copy the server certificate to the client, or use a certificate from a CA the client already trusts.

```sh
BIND_ADDRESS=192.0.2.35 TLS_HOSTNAME=edge.example.com ./airgap-deploy-bundle.sh

curl --cacert /etc/ipa/ocp-metal-ui.crt \
  -X POST https://edge.example.com:3003/api/v1/inspections \
  -H 'Content-Type: application/json' \
  --data-binary @inspection-report.json
```

The API stores the report. It does not configure IPA or replace an Ironic conductor. Point the existing inspection workflow at this URL. No token is required. A `Bearer` header, if an older collector still sends one, is ignored.

## Inspection client contract

The collector is not in this repository. `hw-interrogator` in `build-the-builder` posts hardware reports and does not need a credential.

1. `POST` the inspection JSON to `https://<address>:3003/api/v1/inspections` or `https://<address>:3003/api/v1/inspections/{uuid}`. The deploy serves HTTPS. Use the UI certificate. Do not post to plain HTTP. The address may be `127.0.0.1` or another IP of the UI host.
2. Do not send an API token. If a `Authorization: Bearer` header is still present, the intake routes ignore it and still accept the report. `Content-Type` is also ignored: the body is parsed as JSON even when the header is missing or is not `application/json`. These routes are open to a cross-site simple POST. That request can add a report revision and overwrite the BMC address, serial number, and inventory of an existing node. It cannot read inventory or call any other API. See [Security model / accepted risks](#security-model--accepted-risks).
3. These two routes do not check the `Host` header. Every other route does. That exemption is what lets a collector post by IP even when that IP was not listed in `API_ALLOWED_HOSTS` or `hostname -I`.
4. The body must be a JSON object with an `inventory` object, and a UUID in the body or the URL. The UUID must be a canonical UUID. Reports larger than 2 MiB are rejected. The response is `{"status":"accepted"}` and does not include the stored node.
5. The first report for a UUID creates the node. Another report for the same UUID refreshes the BMC address, serial number, and inventory from that report and is kept as a revision. A blank or missing serial number or BMC address keeps the stored value. A non-blank one overwrites it. Preferences are left unchanged. A retry is not an error.
6. Do not call any other `/api/` path. Those routes require the admin token from `/var/lib/ocp-metal-ui/api.token`.

```sh
curl --cacert /etc/ipa/ocp-metal-ui.crt \
  -X POST https://127.0.0.1:3003/api/v1/inspections \
  -H 'Content-Type: application/json' \
  --data-binary @inspection-report.json
```

## Security model / accepted risks

Inspection intake, `POST /api/v1/inspections` and `POST /api/v1/inspections/{uuid}`, is unauthenticated. That is the owner's decision: a hardware collector posts a report with no token.

Anyone who can reach the server can submit a report that overwrites an existing node's BMC address, serial number, and inventory. A non-blank BMC address or serial number replaces the stored value. A blank or missing one keeps the value already stored. **Generate Ansible Files** writes those hardware facts into the Ansible inventory and host vars. Intake does not change preferences or other operator settings.

Two residuals remain:

- `Content-Type` is not checked. The body is parsed as JSON even when the header is missing or is not `application/json`, so a browser on the network can send a cross-site simple POST that refreshes those hardware facts.
- Each new UUID creates a node and stores report revisions. Flooding intake with made-up node IDs can still use disk. Old revisions for one node are pruned, and that prune does not cap the number of distinct UUIDs.

Firewall port 3003 so only the provisioning network can reach it. Leave `BIND_ADDRESS` on `127.0.0.1` unless collectors on other hosts must post to this UI.

## Generated files

**Generate Ansible Files** writes under `<selected-directory>/ocp-metal-ui/`:

- `inventory.yml` maps each Ansible host name to its serial number and BMC address. Cluster nodes are in the `ocp` group. `dc01` and `dc02` are in the `mgmt` group. Those two names are DNS names, not cluster nodes.
- `host_vars/<host>.yml` contains `serial_number`, `bmc_address`, `node_preferences`, `inventory_source`, and `ironic_uuid` or `redfish_uuid`. Hardware is under `ipa_inventory` or `redfish_inventory`, including every reported disk and NIC. The install disk and the machine-network NIC are the operator's choices in `node_preferences`.
  - `ipv4LastOctet` is written when the operator sets a last octet.
  - `ipv4_ip` is the machine-network address from that octet, or the IPA address when the octet is blank.
  - `roles` lists `master`, `worker`, and `storage` as checked.
  - `role` is the installer value. `master` takes precedence over `worker`.
  - `rendezvous` is written when set, and only a master can hold it.
  - `host_vars/dc01.yml` and `host_vars/dc02.yml` contain the last octet only.
- `group_vars/ocp.yml` holds variables for the `ocp` group, including `apiVip` and `ingressVip` when entered. The DC last octets are not in this file. There is no `group_vars/mgmt.yml`.

The inventory host key is the Ansible `inventory_hostname`. Its `serial_number` variable ties that name to the physical node.

The pull secret and the SSH public key are not written under `ocp-metal-ui/` and are not stored in PostgreSQL. The pull secret stays in the host file. When the operator creates an SSH key, the files are written beside that tree in `cluster-install/`:

- `id_ed25519`, mode 0600
- `id_ed25519.pub`, mode 0644

When the inventory can build a cluster, the host pull secret can be read, and an SSH public key is present, the same action also writes:

- `cluster-install/install-config.yaml`, mode 0600
- `cluster-install/agent-config.yaml`, mode 0644

`install-config.yaml` contains the pull secret because the installer reads that file. A generate that cannot build the cluster removes a previously written pair and leaves the SSH key in place. The host pull secret file is left as it is. Other files in the workspace are left as they are.

The configurator install keeps its profile under `/var/ocp-config/ocp/dev/<profile>/` and writes its own `install-config.yaml` and `agent-config.yaml` under `/opt/ocp/<platform>/<version>/`. Generate refuses a directory inside `/var/ocp-config`, `/opt/ocp`, or `/etc/ansible`. The files from this UI stay in the selected workspace.

The container does not include `ansible-playbook` and does not start a play. The offline Ansible Execution Environment consumes the generated files.

## Blueprint

The Blueprint page collects the base domain, machine network CIDR, network gateway, API VIP, ingress VIP, DNS servers, NTP sources, and the Ansible workspace directory.

These values stay in the saved blueprint and are not fields on that page:

- genesis hostname, genesis IP, genesis NIC, genesis NIC list, genesis storage devices
- management network CIDR, management VLAN, DC01 last octet, DC02 last octet

The OpenShift install uses the base domain, the machine network CIDR, and the genesis IP. Cluster identity, network service lists, registry mirrors, the CA trust bundle, and the management prefix are written to `group_vars/ocp.yml`. The DC last octets are written as `ipv4LastOctet` on `dc01` and `dc02`. The DHCP pool is not one of those variables. The workspace path is saved in PostgreSQL and limits where files may be written. It is not a cluster variable.

Initial defaults, written when the database is first created:

| Key | Value |
| --- | --- |
| `clusterName` | `ocp-baremetal` |
| `baseDomain` | `ocp.internal` |
| `machineNetworkCidr` | `192.0.2.0/24` |
| `networkGateway` | `192.0.2.1` |
| `dnsServers` | `192.0.2.35` |
| `genesisHostname` | `edge` |
| `genesisIp` | `192.0.2.35` |
| `genesisNic` | `eno5` |
| `mgmtNetworkCidr` | `198.51.100.0/24` |
| `mgmtVlan` | `280` |
| `mgmtDc01Octet` | `6` |
| `mgmtDc02Octet` | `7` |

The blueprint stores `edge`. The installed host name is that short name plus the base domain (`edge.ocp.internal` with the defaults above). `genesis` is an alias.

Hosts created before a hardware report have an empty inventory mapping and `node_status: awaiting_ipa`. Linking a placeholder to a discovered node keeps the Ansible host name and replaces the placeholder with that node. Every node uses `DEFAULT_ANSIBLE_GROUP` from `server.py`. The operator does not pick the group.

## Discovered nodes

IPA reports and Redfish retrievals are both discovered nodes, and they stay separate paths. A PXE collector posts to inspection intake with no token. A BMC retrieval is started manually from the UI; the operator enters the username and password for that request, and those credentials are not stored. A Redfish profile is a name and a BMC URL. It does not assign an Ansible host. When both reports carry the same system UUID, they update that one node, and the last report wins. A PXE report marks the node `ipa`. A BMC retrieval marks it `redfish`. A blank BMC address on a later PXE report keeps the stored URL.

On **Discovered Nodes**, link a discovered node to an inventory name or enter a new host name. Links can be staged, unlinked, and saved together. Unlinking returns the inventory name to the available list. Duplicate host names and duplicate node assignments are rejected. Unused names and mistaken discoveries can be deleted. Assignment changes are one database transaction.

After linking, both sources use the same controls for disk, NIC, bond, and VLAN. The left navigation is **Global Cluster Blueprint**, **BMC / iLO Profiles**, **Discovered Nodes**, **Inventory Config**, and **Review**. Blueprint and Inventory Config each save to PostgreSQL, then continue. Review is the step that writes the Ansible files.

## Pull secret and SSH key

The pull secret is a host file. The default path is `/etc/ocp-metal-ui/pull-secret.json`. Set `OCP_METAL_UI_PULL_SECRET_FILE` to use another path. The UI reads that file when it builds a preview or writes `install-config.yaml`, and again after a save. It does not keep a copy in memory between those uses, so replacing the file takes effect on the next use. The image does not contain a pull secret.

On first boot the host creates `/etc/ocp-metal-ui` and writes `pull-secret.json` there, then shreds the password file that decrypted it. To rotate the secret, replace that file or paste a new one on the Blueprint page and save it. The next preview or generate reads the replacement.

The container runs as uid 10001. It does not run as root. Who owns the file, and which mode it uses, is still open. The deploy script records the current placeholder in `PULL_SECRET_OWNER`, `PULL_SECRET_GROUP`, `PULL_SECRET_MODE`, and `PULL_SECRET_DIR_MODE`. The placeholder is uid `10001`, group `10001`, file mode `0600`, and directory mode `0700`, so this container user can read and replace the file. Changing those variables is the whole ownership change. The three choices still in front of David are a `root:ocp-metal-ui` mode `0640` file with `--group-add` and no save, uid `10001` owning a mode `0600` file in a mode `0700` directory, or a podman secret.

The pull secret is plaintext at rest on the host. That is the accepted risk, chosen so the UI can restart unattended.

If the file is missing, is not JSON, or has no usable `auths` object, the UI still starts. A startup log line names the path. The features that need the pull secret return that same error. The log and the error do not include the file contents. A startup log line also warns when the file is group- or world-readable beyond the configured mode.

The Blueprint page can paste a pull secret and save it to that file. The save requires the admin bearer token and HTTPS. It checks that the text is JSON with a non-empty `auths` object. Each entry needs `auth`, or `username` plus `password`, or `token`. `auth` must be valid base64. An empty paste is rejected as empty. The save writes a temporary file in the same directory with mode `0600`, flushes it, replaces the host file, and flushes the directory. The response says that the file was saved and names the path. It does not include the secret. The page does not show the saved secret. The API response for generated install config redacts the pull secret. The file written under `cluster-install/` still contains it, because the installer reads that file.

The Blueprint page also creates or accepts an SSH public key. The private key, when created here, is `cluster-install/id_ed25519`.

The install preview uses the pull secret after the host file can be read. The render play reads `ocp_install_pull_secret_file` and `ocp_install_ssh_public_key_file` only when the matching contents extra var was not passed.

## Redfish

BMC usernames and passwords are entered for a retrieval, sent only over HTTPS, and are not stored or logged. The operator starts that retrieval from the UI. It is not the PXE intake path. The retrieval reads system, processor, memory, storage, drive, and Ethernet-interface details. Missing optional resources are warnings. A missing system serial blocks export of that node until the serial is present. A later PXE report for the same system UUID replaces the BMC inventory, and a later BMC retrieval replaces the PXE inventory. The last report wins.

Generated files stay in the `ocp-metal-ui` directory under the selected workspace. The API checks that the path remains under `/ansible`.

BMC certificates are verified by default. For an untrusted BMC certificate, the operator must accept that certificate for that retrieval. The UI enables retrieval only when its own API is served over HTTPS.

## Build the image

Build on a connected machine with Node.js 22, npm, and Podman or Docker. The image contains the built UI and the Python runtime. It does not need network access after it is built.

The image build does not read or copy a pull secret. Put the plaintext file on the host at `/etc/ocp-metal-ui/pull-secret.json`, or set `OCP_METAL_UI_PULL_SECRET_FILE`, before generating install files.

```sh
npm ci
./airgap-build-bundle.sh
```

The script compiles `dist/`, builds `Containerfile.airgap`, and writes `ocp-metal-ui-engine-bundle.tar`. The image contains no pull secret. Copy that tarball and `airgap-deploy-bundle.sh` to the disconnected host. `package-lock.json` pins the JavaScript dependencies. For a byte-for-byte image rebuild, also pin the Python base image by digest in `Containerfile.airgap`.

## Deploy on the installed host

```sh
./airgap-deploy-bundle.sh
```

The script loads the UI image. It also loads `postgresql:latest` from `/var/www/html/pub/images/postgresql/postgresql.tar` when that image is not already loaded. The tar's own name is `registry.access.redhat.com/hi/postgresql:latest`. The script tags it `postgresql:latest` and starts container `ocp-metal-ui-db`. The data directory is `/var/lib/ocp-metal-ui/postgres`, owned by uid 999. The database listens on `127.0.0.1:5433`. The database name and login role are `ocp_metal_ui`. The first start writes `/var/lib/ocp-metal-ui/db.env`, mode 0600. That file holds the role password and the PostgreSQL superuser password. The UI does not connect as the superuser. Quay later uses the same image as `postgresql-quay` on port 5432 and the `quay` database. The two data directories are separate.

Replacing the UI container does not delete `/var/lib/ocp-metal-ui/postgres` or `db.env`. Back those up together. A newer major version of `postgresql.tar` has to match the `PG_VERSION` file in that directory. Keep the tar that initialized it. The host directory `ansible-workspace` is mounted at `/ansible`. The operator can keep that directory or choose another absolute path inside the mount.

The database holds nodes, placeholders, preferences, blueprint settings, and Redfish profile names. It does not hold BMC passwords. No sample BMC profiles are loaded. The UI listens on `BIND_ADDRESS` (default `127.0.0.1`) and port `3003`.

The first deploy writes two token files outside the git checkout, mode 0400, owned by uid 10001:

| File | Use |
| --- | --- |
| `/var/lib/ocp-metal-ui/api.token` | Admin token. Paste it into the UI **API token** field and choose **Use token**. The browser keeps it in `sessionStorage` for the tab. |

The container reads the admin token from `/run/secrets/api-token`. Delete the file before the next deploy to rotate it. Set `API_TOKEN_FILE` to use a different path. Hardware inspection posts do not use this token.

Every `/api/` route except `GET /api/v1/health` and the two inspection intake routes requires the admin token. The `Host` header must be `localhost`, `127.0.0.1`, `::1`, `TLS_HOSTNAME`, or a name in `API_ALLOWED_HOSTS`, except on inspection intake, which does not check `Host`. The deploy forwards `API_ALLOWED_HOSTS` into the container and prints the allowlist. When `BIND_ADDRESS` is `0.0.0.0` or `::`, it also allows the addresses from `hostname -I`, so the UI can be opened by the host IP. Leave the bind on `127.0.0.1` unless that is what you want. `BIND_ADDRESS` still overrides it. Binding any other address without TLS prints a warning.

To mount a different workspace directory:

```sh
ANSIBLE_HOST_DIR=/srv/ansible-workspace ./airgap-deploy-bundle.sh
```

The script grants group write on the workspace root and runs the container as that directory's group. Use a dedicated directory, or review that permission change before pointing it at a shared directory. On **Generate Ansible Files**, inventory and host vars are written under `<selected-directory>/ocp-metal-ui/`, and the preferences are saved in PostgreSQL as part of that action.

## Deploy on this workstation

The script prepares `/var/lib/ocp-metal-ui/postgres` and starts the database container. Run it as root. `postgresql:latest` must already be loaded, or `POSTGRES_IMAGE_TAR` must point at the image archive.

```sh
BIND_ADDRESS=127.0.0.1 HOST_PORT=18080 ./airgap-deploy-bundle.sh
```

Open `https://edge.example.com:18080` when the certificate was issued for that name. Open `https://127.0.0.1:18080` when the certificate covers that address. Some systems resolve `localhost` to IPv6. The published port is IPv4.

## HTTPS

The deploy script always enables HTTPS. If no certificate is supplied, it creates a self-signed certificate and key in `~/.config/ocp-metal-ui/tls/` (or `$TLS_CERT_DIR`). The certificate includes `localhost` and the host's fully qualified name, and is valid for 365 days. Later runs reuse it. OpenSSL 1.1.1 or later is required. To rotate the certificate or change `TLS_HOSTNAME`, use a new `TLS_CERT_DIR`.

```sh
TLS_HOSTNAME=ui.example.internal ./airgap-deploy-bundle.sh
```

`TLS_HOSTNAME` must be a DNS name on the certificate and must resolve to the host.

To supply an existing certificate:

```sh
TLS_CERT_HOST_FILE=/etc/ocp-metal-ui/ui.crt \
TLS_KEY_HOST_FILE=/etc/ocp-metal-ui/ui.key \
./airgap-deploy-bundle.sh
```

The script checks that the certificate is valid, matches `TLS_HOSTNAME`, and matches the unencrypted private key before it stops an existing container. The deploy does not download a certificate. Keep the private key on the host.

A browser warns until a self-signed certificate is trusted. Distribute the generated certificate to operator workstations, or use a certificate from your CA. Do not disable certificate checks in the browser.

### Trust a local test certificate in Chrome

Import the certificate as a peer. Do not mark it as a CA.

```sh
certutil -A \
  -d sql:"$HOME/.pki/nssdb" \
  -n "ocp-metal-ui-local-test" \
  -t "P,," \
  -i "$HOME/.config/ocp-metal-ui-test/tls/tls.crt"
```

Restart Chrome, then open the workstation URL. When the certificate is replaced, delete the old peer entry first:

```sh
certutil -D \
  -d sql:"$HOME/.pki/nssdb" \
  -n "ocp-metal-ui-local-test"
```

### Smoke test

From the project directory:

```sh
TLS_CA_FILE="$HOME/.config/ocp-metal-ui-test/tls/tls.crt" \
  ./airgap-smoke-test.sh
```

The script checks TLS, the health, config, and inventory APIs, the page's JavaScript and CSS, and a headless Chrome render of the landing page and the discovered-node inventory. With `TLS_CA_FILE`, Chrome pins that certificate in a temporary profile. Without it, Chrome must already trust the server certificate. The script does not skip certificate checks.

Optional variables: `BASE_URL` for another HTTPS address, `CHROME_BIN` for the Chrome binary, `BROWSER_TIMEOUT` for the headless timeout. The script default for `BASE_URL` is `https://127.0.0.1:3003`. For the workstation container set `BASE_URL=https://127.0.0.1:18080`.

## Local development

Start the API and the UI in two terminals:

```sh
DATABASE_URL='postgresql://ocp_metal_ui:PASSWORD@127.0.0.1:5433/ocp_metal_ui?sslmode=disable' \
ANSIBLE_WORKING_DIR=./ansible-workspace \
API_TOKEN='local-dev-token' \
python3 server.py
npm run dev
```

`python3 server.py` listens on `127.0.0.1` and port 3003 unless `HOST` or `PORT` is set. Set `API_TOKEN`, or `API_TOKEN_FILE` to a mode 0600 file outside the checkout. Paste the admin token into the UI. A missing or unreadable admin token file fails closed: protected routes return 503. Inspection posts do not need a token. This process uses HTTP unless TLS certificate files are configured. The container deployment uses HTTPS. Vite proxies `/api` to `http://localhost:3003`.

```sh
npm run lint
npm run build
npm run test:backend
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Readiness |
| `GET` | `/api/v1/config` | Ansible workspace, saved destination, and blueprint |
| `GET` | `/api/v1/nodes` | Inventory and saved preferences |
| `PUT` | `/api/v1/redfish/endpoints` | Save BMC profiles. No credentials. |
| `POST` | `/api/v1/redfish/retrieve` | Retrieve and store BMC inventory. HTTPS required. |
| `POST` | `/api/v1/nodes` | Create a named placeholder host |
| `POST` | `/api/v1/nodes/link` | Link a placeholder to a reported node |
| `POST` | `/api/v1/nodes/assignments` | Link or unlink discovered nodes and inventory names |
| `DELETE` | `/api/v1/nodes/{uuid}` | Delete a node or inventory name |
| `POST` | `/api/v1/inspections` | Store a report that includes `uuid` and `inventory` |
| `POST` | `/api/v1/inspections/{uuid}` | Store a report. UUID is in the path. |
| `PUT` | `/api/v1/nodes/preferences` | Save `{ "preferences": { "<uuid>": { ... } } }` |
| `PUT` | `/api/v1/configuration` | Save the blueprint, workspace destination, and node preferences |
| `POST` | `/api/v1/ansible-files` | Write the reviewed inventory and variables into the workspace |

Every `/api/` route except `GET /api/v1/health` and inspection intake requires the admin bearer token. Inspection intake accepts reports with no token and does not check `Host`. A later report overwrites that node's BMC address and serial number when the new value is non-blank, and refreshes inventory. A blank or missing serial or BMC address keeps the stored value. HTTPS protects credentials in transit. The `Host` header is checked on every other route so a DNS-rebinding name cannot call the API. Firewall the UI port to the provisioning network. See [Security model / accepted risks](#security-model--accepted-risks).

Generated host vars contain hardware inventory. Handle that data under your organization's policy. Keep secrets in Ansible Vault. Redfish credentials are not written to disk.
