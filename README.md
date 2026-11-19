# OpenShift Bare-Metal Builder

Author: David Stratford, assisted by AI.

One container serves the PatternFly UI and its API. The operator reviews discovered hardware, sets disk and network choices, and writes Ansible files to a mounted workspace. The UI reads blueprint defaults, named hosts, and discovered hardware from PostgreSQL. It does not ship sample inventory. The node serial number is the hardware identifier that links a discovered system to its Ansible host.

## Ports

| Where | Address | Port |
| --- | --- | --- |
| Example installed host (`genesisIp`) | `192.0.2.35` | `3003` |
| This workstation | `127.0.0.1` | `18080` |

Port 3003 is the UI on the installed host. Semaphore on that host uses port 3000. The hardware collector on that host uses port 5000 and forwards reports to `http://127.0.0.1:3003`. Boot files are served by httpd on port 80.

The workstation container is a separate process. Open it at `https://edge.example.com:18080` when the certificate was issued for that name.

`./airgap-deploy-bundle.sh` listens on `BIND_ADDRESS` (default `0.0.0.0`) and port `3003`. To bind the example genesis address, set `BIND_ADDRESS=192.0.2.35`. For the workstation:

```sh
BIND_ADDRESS=127.0.0.1 HOST_PORT=18080 ./airgap-deploy-bundle.sh
```

## Data flow

1. An inspection client sends a node UUID and `inventory` JSON to `POST /api/v1/inspections`, or to `POST /api/v1/inspections/{uuid}`.
2. The API upserts the report in PostgreSQL. A later report for the same UUID refreshes inventory and keeps the operator's preferences.
3. The UI polls `GET /api/v1/nodes` every 15 seconds.
4. The operator can create named placeholder hosts before reports arrive, then link each name to a discovered node. The inventory group is set in application configuration.
5. **Review and confirm** lists discovered nodes and named placeholders. The operator can refresh before confirming.
6. Blueprint and Inventory Config can each save the current configuration to PostgreSQL and continue. **Generate Ansible Files** on Review writes the inventory and variables. It does not run Ansible.
7. If PXE or IPA is unavailable, the operator can enter BMC HTTPS URLs and retrieve inventory from HPE iLO, Dell iDRAC, Supermicro, or another Redfish BMC. Those systems are stored in the same inventory and use the same assignment workflow.

The home page links to the cluster blueprint and to BMC profiles. Blueprint, inventory, and review are separate URLs. Browser Back and Forward move among those pages.

## Inspection reports

The body matches an Ironic inspection report: a JSON object with an `inventory` object and a node `uuid`. The UUID may be in the URL instead. `bmc_address` is optional.

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
curl --cacert /etc/ipa/ocp-metal-ui.crt \
  -X POST https://edge.example.com:3003/api/v1/inspections \
  -H 'Content-Type: application/json' \
  --data-binary @inspection-report.json
```

The API stores the report. It does not configure IPA or replace an Ironic conductor. Point the existing inspection workflow at this URL.

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

The pull secret and the SSH public key are not written under `ocp-metal-ui/` and are not stored in PostgreSQL. When the operator pastes a pull secret or creates an SSH key, the files are written beside that tree in `cluster-install/`:

- `pull-secret.vault`, mode 0600, Ansible vault variable `ocp_pre_install_pull_secret_contents`
- `id_ed25519`, mode 0600
- `id_ed25519.pub`, mode 0644

When the inventory can build a cluster, a pull secret can be opened, and an SSH public key is present, the same action also writes:

- `cluster-install/install-config.yaml`, mode 0600
- `cluster-install/agent-config.yaml`, mode 0644

`install-config.yaml` contains the opened pull secret because the installer reads that file. The vault password stays in its own file. A generate that cannot build the cluster removes a previously written pair and leaves the pull secret and the SSH key in place. Other files in the workspace are left as they are.

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

IPA reports and Redfish retrievals are both discovered nodes. A Redfish profile is a name and a BMC URL. It does not assign an Ansible host.

On **Discovered Nodes**, link a discovered node to an inventory name or enter a new host name. Links can be staged, unlinked, and saved together. Unlinking returns the inventory name to the available list. Duplicate host names and duplicate node assignments are rejected. Unused names and mistaken discoveries can be deleted. Assignment changes are one database transaction.

After linking, both sources use the same controls for disk, NIC, bond, and VLAN. The left navigation is **Global Cluster Blueprint**, **BMC / iLO Profiles**, **Discovered Nodes**, **Inventory Config**, and **Review**. Blueprint and Inventory Config each save to PostgreSQL, then continue. Review is the step that writes the Ansible files.

## Pull secret and SSH key

The image build copies an Ansible vault file to `/app/pull-secret.vault`, mode 0600. That file is produced on the build machine and is not stored in git. The vault password is mounted at `/vault/password` and is not in the image. On the Blueprint page, paste a pull secret or choose a file to store one for this workspace, or create an SSH key. The saved vault file is `cluster-install/pull-secret.vault`. Leave the field blank to keep the pull secret built into the image.

The install preview shows the pull secret after the vault password can open it. The render play reads `ocp_install_pull_secret_file` and `ocp_install_ssh_public_key_file` only when the matching contents extra var was not passed.

## Redfish

BMC usernames and passwords are entered for a retrieval, sent only over HTTPS, and are not stored or logged. The retrieval reads system, processor, memory, storage, drive, and Ethernet-interface details. Missing optional resources are warnings. A missing system serial blocks export of that node until the serial is present.

Generated files stay in the `ocp-metal-ui` directory under the selected workspace. The API checks that the path remains under `/ansible`.

BMC certificates are verified by default. For an untrusted BMC certificate, the operator must accept that certificate for that retrieval. The UI enables retrieval only when its own API is served over HTTPS.

## Build the image

Build on a connected machine with Node.js 22, npm, and Podman or Docker. The image contains the built UI and the Python runtime. It does not need network access after it is built.

The cluster pull secret is not in this repository. On the build machine, place the plaintext pull secret at `pull-secret.json` in this directory and set `VAULT_PASSWORD_FILE` to the Ansible vault password file. Neither file is committed. `airgap-build-bundle.sh` writes `pull-secret.vault` beside them, mode 0600, as the Ansible vault variable `ocp_pre_install_pull_secret_contents`. That vault file is gitignored. The image copies it to `/app/pull-secret.vault`. The plaintext pull secret and the vault password are not copied into the image.

Mount the same password file at `/vault/password` when the container starts. To use a different secret later, paste it on the Blueprint page or replace `pull-secret.json` and build again. A secret saved from the UI is `cluster-install/pull-secret.vault` in the Ansible workspace, and it overrides the copy in the image for that workspace.

```sh
npm ci
VAULT_PASSWORD_FILE=/path/to/vault-password ./airgap-build-bundle.sh
```

The script compiles `dist/`, vaults `pull-secret.json`, builds `Containerfile.airgap`, and writes `ocp-metal-ui-engine-bundle.tar`. Copy that tarball and `airgap-deploy-bundle.sh` to the disconnected host. `package-lock.json` pins the JavaScript dependencies. For a byte-for-byte image rebuild, also pin the Python base image by digest in `Containerfile.airgap`.

## Deploy on the installed host

```sh
./airgap-deploy-bundle.sh
```

The script loads the UI image. It also loads `postgresql:latest` from `/var/www/html/pub/images/postgresql/postgresql.tar` when that image is not already loaded. The tar's own name is `registry.access.redhat.com/hi/postgresql:latest`. The script tags it `postgresql:latest` and starts container `ocp-metal-ui-db`. The data directory is `/var/lib/ocp-metal-ui/postgres`, owned by uid 999. The database listens on `127.0.0.1:5433`. The database name and login role are `ocp_metal_ui`. The first start writes `/var/lib/ocp-metal-ui/db.env`, mode 0600. That file holds the role password and the PostgreSQL superuser password. The UI does not connect as the superuser. Quay later uses the same image as `postgresql-quay` on port 5432 and the `quay` database. The two data directories are separate.

Replacing the UI container does not delete `/var/lib/ocp-metal-ui/postgres` or `db.env`. Back those up together. A newer major version of `postgresql.tar` has to match the `PG_VERSION` file in that directory. Keep the tar that initialized it. The host directory `ansible-workspace` is mounted at `/ansible`. The operator can keep that directory or choose another absolute path inside the mount.

The database holds nodes, placeholders, preferences, blueprint settings, and Redfish profile names. It does not hold BMC passwords. No sample BMC profiles are loaded. The UI listens on the address in `BIND_ADDRESS` (default `0.0.0.0`) and port `3003`.

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
python3 server.py
npm run dev
```

`python3 server.py` listens on port 3003 unless `PORT` is set. This process uses HTTP unless TLS certificate files are configured. The container deployment uses HTTPS. Vite proxies `/api` to `http://localhost:3003`.

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

The API has no application login. HTTPS protects Redfish credentials in transit. It does not authenticate the operator. Keep the service on a network you control.

Generated host vars contain hardware inventory. Handle that data under your organization's policy. Keep secrets in Ansible Vault. Redfish credentials are not written to disk.
