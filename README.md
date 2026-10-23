# OpenShift Bare-Metal Builder

This application serves the React/PatternFly interface and a small SQLite-backed
API from one container. The browser reads discovered nodes from the API, lets an
operator choose disk and network preferences, and saves the reviewed
configuration to SQLite and a mounted Ansible workspace. Blueprint defaults,
operator-named hosts, and discovered hardware are served from SQLite; the UI
does not embed mock inventory. A node's reported serial number is the hardware
identifier that links each discovered system to its Ansible inventory host.

## Data flow

1. An Ironic inspection/reporting integration sends each node's UUID and
   `inventory` JSON to `POST /api/v1/inspections` (or sends it to
   `POST /api/v1/inspections/{uuid}`).
2. The API upserts the report into SQLite. A later report for the same UUID
   refreshes inventory while preserving the operator's preferences.
3. The UI polls `GET /api/v1/nodes` every 15 seconds and displays current
   hardware inventory.
4. Operators can create named placeholder hosts before any reports arrive,
   assign host names, and later link each name to a discovered node. The
   inventory group is fixed in application configuration.
5. Operators review all discovered nodes and named placeholders on a dedicated
   **Review and confirm** page. The page summarizes inventory readiness and
   allows a refresh before confirmation.
6. The Blueprint and Inventory Config pages can each commit the current
   configuration to SQLite, then continue to the next workflow page. Review
   provides the final **Generate Ansible Files** action, which writes the
   inventory and variables to the configured workspace. It does not run
   Ansible; a separately delivered Execution Environment can use the files.
7. If PXE/IPA is unavailable, the Redfish discovery workflow lets an operator
   enter BMC HTTPS URLs and retrieve inventory from HPE iLO, Dell iDRAC,
   Supermicro, or another Redfish-compatible BMC. Retrieved systems are stored
   in the same SQLite inventory as IPA reports and continue through the same
   assignment and hardware configuration workflow.

The home page introduces the hardware-discovery and OpenShift-install
configuration workflow and links directly to the cluster blueprint or BMC/iLO
profiles. Workflow stages are URL-addressable and participate in browser
history; browser Back and Forward move among blueprint, inventory/BMC, and
review pages.

The report body follows the Ironic inspection shape: a JSON object with an
`inventory` object and a node `uuid` (the UUID may instead be in the URL).
`bmc_address` is optional. The node serial is read from the top-level
`serial_number`, `inventory.serial_number`,
`inventory.system_vendor.serial_number` (preferred), or
`inventory.system.serial_number`. Discovered nodes require a serial number
before they can be committed to the Ansible inventory. Named placeholders can
be committed before the hardware reports arrive. For example:

```json
{
  "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
  "bmc_address": "10.10.50.21",
  "inventory": {
    "system_vendor": { "serial_number": "NODE-SERIAL-01" },
    "cpu": { "count": 64, "model_name": "Example CPU" },
    "memory": { "physical_mb": 262144 },
    "disks": [
      { "name": "/dev/nvme0n1", "serial": "EXAMPLE123", "model": "Example NVMe", "size": 1000000000000 }
    ],
    "interfaces": [
      { "name": "eno1", "mac_address": "52:54:00:fa:19:bc" }
    ]
  }
}
```

For serials provided outside the inventory object, a top-level field is also
accepted:

```json
{
  "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
  "serial_number": "NODE-SERIAL-01",
  "inventory": {
    "cpu": { "count": 64, "model_name": "Example CPU" }
  }
}
```

```sh
curl -X POST http://ui-host:8080/api/v1/inspections \
  -H 'Content-Type: application/json' \
  --data-binary @inspection-report.json
```

The API accepts an inspection report; it does not replace the Ironic conductor
or configure IPA to report here automatically. Configure the existing
inspection/reporting workflow (or a trusted-network callback) to forward the
report to this API.

The generated files are written under `<selected-directory>/ocp-metal-ui/`:

- `inventory.yml`: maps the selected Ansible host name to the reported serial number
  and BMC address, grouped under the configured `baremetal` inventory group.
- `host_vars/<host>.yml`: includes `serial_number`, `bmc_address`,
  operator-selected `node_preferences`, `inventory_source`, `ironic_uuid` for
  IPA or `redfish_uuid` for Redfish, and source-specific
  inventory under `ipa_inventory` or `redfish_inventory`.
- `group_vars/baremetal.yml`: includes the non-secret cluster blueprint for
  the configured inventory group.

The inventory host key is the stable Ansible `inventory_hostname`; its
`serial_number` variable ties that host to the physical node reported by IPA or
Redfish.
The Global Cluster Blueprint includes the machine network CIDR, DNS servers,
NTP sources, and the selected Ansible workspace directory. Cluster identity,
network service lists, registry mirrors, and the CA trust bundle are exported
under `cluster_blueprint` in `group_vars/baremetal.yml`. The workspace choice is
saved in SQLite and constrains where generated files may be written; it is not
included as a cluster variable.
Hosts created before hardware reporting appear with an empty inventory host mapping
and a `node_status: awaiting_ipa` host var. When the operator links a placeholder
to a discovered node, the placeholder is replaced by that node while preserving
its Ansible host name. All nodes use the inventory group configured
in the application (`DEFAULT_ANSIBLE_GROUP` in `server.py`); it is intentionally
not an operator-selectable field.
Both IPA inspection reports and manually retrieved Redfish hardware are stored
as discovered nodes in the same SQLite inventory. Redfish profiles contain only
a profile name and BMC URL; they do not assign an Ansible host. From either
discovery method, use the shared **Discovered Nodes** page to link an available
discovered node to an inventory name, or enter a new host name. Inventory names
can also be linked from their own rows. Links can be staged, unlinked, and saved
together; unlinking restores the inventory name as an available item. Duplicate
host names and duplicate node assignments are rejected. Mistaken discoveries
and unused inventory names can be deleted. Assignment changes are validated and
saved as one SQLite transaction. After linking, both discovery sources use the
same hardware configuration tiles for disk, NIC, bond, and VLAN selections.
Once in the shared
workflow, the persistent left navigation provides access to **Global Cluster
Blueprint**, **BMC / iLO Profiles**, **Discovered Nodes**, **Inventory Config**,
and **Review**. The Blueprint and Inventory Config pages each provide a commit
action that saves the current blueprint and node preferences to SQLite without
generating files, followed by a separate Continue action. Review remains the
final confirmation before generating Ansible inventory and variable files.
Use the generated `host_vars` for node-specific hardware and preferences, and
the group's `group_vars` file for shared cluster configuration. Secret values
such as pull secrets and BMC passwords are not included in generated files;
define them separately in Ansible Vault and reference the vaulted variables
from your playbooks. Redfish BMC URLs and profile names are stored in SQLite.
BMC usernames and passwords are entered only when
inventory is retrieved, are sent only over HTTPS, and are not stored or
logged. The Redfish workflow retrieves system, processor, memory, storage /
drive, and Ethernet-interface details. Missing optional resources are reported
as warnings; a missing system serial prevents exporting that node until the
serial is available. Generated files are confined to an app-owned
`ocp-metal-ui` subdirectory under the selected workspace directory; other
playbooks and files are not modified.

BMC certificates are verified by default. For an untrusted BMC certificate,
the operator must explicitly accept the risk for that retrieval. The UI/API
itself must use HTTPS before the retrieval action is enabled.

## Build an air-gap bundle

Build on a connected machine with Node.js 22, npm, and Podman or Docker. The
container image contains the built UI, Python's standard-library SQLite API,
and the Python runtime; it needs no internet access once deployed.

```sh
npm ci
./airgap-build-bundle.sh
```

The build script compiles `dist/`, builds `Containerfile.airgap`, and writes
`ocp-metal-ui-engine-bundle.tar`. Transfer that tarball and
`airgap-deploy-bundle.sh` into the disconnected environment. The npm lockfile
pins JavaScript dependencies; for byte-for-byte reproducible image builds, also
pin the Python base image by digest in `Containerfile.airgap`.

## Run in a disconnected container

```sh
./airgap-deploy-bundle.sh
```

The script imports the image and runs it on port 8080. SQLite is stored in the
named `ocp-metal-ui-data` volume, so replacing/restarting the application
container does not delete inventory or preferences. Back up this volume using
the container engine's normal volume backup procedure. The host's
`ansible-workspace` directory is mounted at `/ansible` inside the container by
default. The UI lets the operator confirm that directory or choose another
absolute path inside the mounted workspace.

The SQLite database file is `/data/ocp-metal-ui.sqlite3` inside the container.
On the container host, its storage is managed by the named volume
`ocp-metal-ui-data` (inspect its mount location with
`podman volume inspect ocp-metal-ui-data` or `docker volume inspect
ocp-metal-ui-data`). Nodes, named placeholders, preferences, and app settings (including initial
blueprint defaults and manually entered Redfish BMC profiles) are stored there.
The database is separate from the Ansible workspace. No sample BMC profiles
are preloaded.

To mount a different host directory:

```sh
ANSIBLE_HOST_DIR=/srv/ansible-workspace ./airgap-deploy-bundle.sh
```

The deploy script grants group write access on the selected workspace root and
uses the host directory's group for the container process. Choose a dedicated
workspace or review that permission change if using an existing shared
directory. The UI validates that selected output paths remain within `/ansible`.
On **Generate Ansible Files**, generated inventory, group vars, and host vars
are written under `<selected-directory>/ocp-metal-ui/`; SQLite preferences are
committed as part of that operation. Other
files and playbooks in the workspace are left alone. The UI container does not
include `ansible-playbook` and does not start a playbook. This keeps the UI
independent of the separately versioned, offline-delivered Ansible Execution
Environment, which can consume the generated inventory path.

To use a different host port, set `HOST_PORT` when running the deploy script:

```sh
HOST_PORT=18080 ./airgap-deploy-bundle.sh
```

### Enable HTTPS for Redfish retrieval

Create or provision a server certificate and private key on the disconnected
host before deployment. The certificate should be trusted by operator
browsers and match the host name used in the browser. Mount both files read-only
by setting their paths when running the deploy script:

```sh
TLS_CERT_HOST_FILE=/etc/ocp-metal-ui/ui.crt \
TLS_KEY_HOST_FILE=/etc/ocp-metal-ui/ui.key \
./airgap-deploy-bundle.sh
```

The app then serves HTTPS on the configured host port (8080 by default).
Deployment performs no internet access or certificate download. Keep the
private key protected on the host. Without both files, the application serves
HTTP and Redfish retrieval is disabled; the IPA inventory workflow remains
available on the trusted management network.

## Local development and checks

Start the API and UI in separate terminals:

```sh
DATABASE_PATH=./data/ocp-metal-ui.sqlite3 \
ANSIBLE_WORKING_DIR=./ansible-workspace \
python3 server.py
npm run dev
```

Vite proxies `/api` requests to the local API on port 8080. Run checks with:

```sh
npm run lint
npm run build
npm run test:backend
```

## API summary

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Container readiness |
| `GET` | `/api/v1/config` | Configured Ansible workspace, saved destination, and blueprint |
| `GET` | `/api/v1/nodes` | Read inventory and saved preferences |
| `PUT` | `/api/v1/redfish/endpoints` | Save manually entered BMC profiles (no credentials) |
| `POST` | `/api/v1/redfish/retrieve` | Retrieve and store BMC inventory; HTTPS required |
| `POST` | `/api/v1/nodes` | Create an operator-named placeholder host |
| `POST` | `/api/v1/nodes/link` | Link a named placeholder to a reported IPA node |
| `POST` | `/api/v1/nodes/assignments` | Batch link or unlink discovered nodes and inventory names |
| `DELETE` | `/api/v1/nodes/{uuid}` | Delete a node or inventory name |
| `POST` | `/api/v1/inspections` | Ingest report with `uuid` and `inventory` |
| `POST` | `/api/v1/inspections/{uuid}` | Ingest report with UUID supplied in the path |
| `PUT` | `/api/v1/nodes/preferences` | Save `{ "preferences": { "<uuid>": { ... } } }` |
| `PUT` | `/api/v1/configuration` | Batch-save the blueprint, workspace destination, and node preferences to SQLite |
| `POST` | `/api/v1/ansible-files` | Commit reviewed preferences and generated inventory/vars into workspace |

The API has no application-level authentication. Without TLS certificates it
uses plain HTTP and Redfish retrieval is disabled. Do not expose it to
untrusted networks; protect access with network segmentation/firewall policy.
When TLS is configured, HTTPS protects Redfish credentials in transit but
does not add user authentication.

The initial cluster blueprint defaults are seeded in SQLite when the database
is first initialized. Blueprint and node preference changes can be saved to SQLite before review.
The final review action generates files and persists the reviewed configuration;
Redfish profiles are saved separately and before retrieval.
Generated host vars contain discovered hardware inventory, which
may include infrastructure details; handle it according to your organization's
data policy. Keep secrets in Ansible Vault rather than in SQLite or generated files.
Redfish credentials are transient and never persisted.
