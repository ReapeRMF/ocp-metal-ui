import json
import http.client
import os
import re
import sqlite3
import ssl
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse


APP_DIR = Path(__file__).resolve().parent
DIST_DIR = APP_DIR / "dist"
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/data/ocp-metal-ui.sqlite3"))
ANSIBLE_WORKING_DIR = Path(os.environ.get("ANSIBLE_WORKING_DIR", "/ansible"))
TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE")
TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE")
MAX_REQUEST_BYTES = 5 * 1024 * 1024
ANSIBLE_OUTPUT_SUBDIR = "ocp-metal-ui"
ANSIBLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFERENCE_FIELDS = {
    "rootDisk",
    "netType",
    "singleNic",
    "singleVlanEnabled",
    "singleVlanId",
    "bondNics",
    "bondVlanEnabled",
    "bondVlanId",
    "ansibleHost",
}
DEFAULT_ANSIBLE_GROUP = "baremetal"
BLUEPRINT_FIELDS = {
    "clusterName",
    "baseDomain",
    "controlPlaneTopology",
    "mirrorRegistries",
    "caCertBundle",
    "machineNetworkCidr",
    "dnsServers",
    "ntpServers",
}
DEFAULT_REDFISH_ENDPOINTS = []


class RedfishError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def validate_redfish_url(value):
    if not isinstance(value, str):
        raise ValueError("BMC Redfish URL must be a string.")
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("BMC Redfish URL must be an HTTPS URL with a hostname.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("BMC Redfish URL cannot contain credentials, query, or fragment.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("BMC Redfish URL has an invalid port.") from None
    return value.strip().rstrip("/")


class RedfishClient:
    def __init__(self, base_url, allow_untrusted_tls=False, timeout=15):
        self.base_url = validate_redfish_url(base_url)
        self.origin = urlparse(self.base_url)
        self.timeout = timeout
        self.allow_untrusted_tls = allow_untrusted_tls
        if allow_untrusted_tls:
            self.ssl_context = ssl._create_unverified_context()
        else:
            self.ssl_context = ssl.create_default_context()
        self.token = None
        self.session_url = None

    def request(self, method, resource, body=None, headers=None):
        url = urljoin(self.base_url + "/", resource)
        parsed = urlparse(url)
        try:
            same_origin = (
                parsed.hostname == self.origin.hostname
                and (parsed.port or 443) == (self.origin.port or 443)
            )
        except ValueError:
            raise RedfishError("BMC returned a Redfish resource with an invalid port.") from None
        if parsed.scheme != "https" or not same_origin or parsed.username or parsed.password:
            raise RedfishError("BMC returned a Redfish resource outside its HTTPS origin.")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=self.timeout, context=self.ssl_context
        )
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if self.token:
            request_headers["X-Auth-Token"] = self.token
        if headers:
            request_headers.update(headers)
        encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=encoded_body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read(2 * 1024 * 1024 + 1)
            if len(response_body) > 2 * 1024 * 1024:
                raise RedfishError("BMC response exceeds the 2 MiB resource limit.")
            if response.status < 200 or response.status >= 300:
                detail = ""
                if response_body:
                    try:
                        parsed_error = json.loads(response_body)
                        error = parsed_error.get("error", {})
                        detail = error.get("message", "") if isinstance(error, dict) else ""
                    except (json.JSONDecodeError, AttributeError):
                        pass
                suffix = f": {detail}" if detail else ""
                raise RedfishError(
                    f"Redfish {method} failed with HTTP {response.status}{suffix}.",
                    status=response.status,
                )
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if not response_body:
                return {}, response_headers
            try:
                result = json.loads(response_body)
            except json.JSONDecodeError:
                raise RedfishError("BMC returned a non-JSON Redfish response.") from None
            if not isinstance(result, dict):
                raise RedfishError("BMC returned an invalid Redfish resource.")
            return result, response_headers
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise RedfishError(f"Unable to connect to BMC Redfish service: {error}") from None
        finally:
            connection.close()

    def get(self, resource):
        return self.request("GET", resource)[0]

    def collection_members(self, resource, warnings, label):
        if (
            not isinstance(resource, dict)
            or not isinstance(resource.get("@odata.id"), str)
            or not resource["@odata.id"]
        ):
            warnings.append(f"{label} resource is not available.")
            return []
        try:
            collection = self.get(resource["@odata.id"])
        except RedfishError as error:
            if error.status in {404, 501}:
                warnings.append(f"{label} resource was not found.")
                return []
            raise
        members = collection.get("Members")
        if not isinstance(members, list):
            warnings.append(f"{label} collection did not contain a Members list.")
            return []
        results = []
        for member in members:
            if isinstance(member, dict) and isinstance(member.get("@odata.id"), str):
                try:
                    results.append(self.get(member["@odata.id"]))
                except RedfishError as error:
                    if error.status in {404, 501}:
                        warnings.append(f"A {label.lower()} resource was not found.")
                        continue
                    raise
        return results

    def retrieve(self, username, password):
        root = self.get("/redfish/v1/")
        session_service = root.get("SessionService", {})
        if not isinstance(session_service, dict):
            session_service = {}
        session_resource = session_service.get("@odata.id", "/redfish/v1/SessionService")
        if not isinstance(session_resource, str):
            raise RedfishError("BMC returned an invalid Redfish session service link.")
        sessions_resource = session_service.get("Sessions", {})
        if isinstance(sessions_resource, dict):
            sessions_resource = sessions_resource.get("@odata.id")
        if not isinstance(sessions_resource, str):
            sessions_resource = session_resource.rstrip("/") + "/Sessions"
        sessions_url = urljoin(self.base_url + "/", sessions_resource)
        session, session_headers = self.request(
            "POST",
            sessions_url,
            {"UserName": username, "Password": password},
        )
        self.token = session_headers.get("x-auth-token")
        session_location = session_headers.get("location") or session.get("@odata.id")
        if isinstance(session_location, str):
            self.session_url = session_location

        warnings = []
        try:
            if not self.token:
                raise RedfishError("BMC did not return a Redfish session token.")
            systems = self.collection_members(
                root.get("Systems"), warnings, "Computer systems"
            )
            if not systems:
                raise RedfishError("No computer systems were returned by the BMC.")
            if len(systems) != 1:
                raise RedfishError(
                    "This BMC reports multiple computer systems; select a system-specific endpoint."
                )
            system = systems[0]
            cpu_summary = system.get("ProcessorSummary", {})
            if not isinstance(cpu_summary, dict):
                cpu_summary = {}
            processors = self.collection_members(system.get("Processors"), warnings, "Processors")
            cpu_count = cpu_summary.get("Count")
            if not isinstance(cpu_count, int) or cpu_count <= 0:
                cpu_count = len(processors)
            cpu_models = list(
                dict.fromkeys(
                    processor.get("Model")
                    for processor in processors
                    if isinstance(processor.get("Model"), str) and processor["Model"]
                )
            )
            cpu_model = ", ".join(cpu_models) or cpu_summary.get("Model") or "Unknown CPU"
            if not cpu_count:
                warnings.append("Processor count was not reported by the BMC.")

            memory_summary = system.get("MemorySummary", {})
            if not isinstance(memory_summary, dict):
                memory_summary = {}
            memory_total = 0
            if isinstance(memory_summary.get("TotalSystemMemoryGiB"), (int, float)):
                memory_total = int(memory_summary["TotalSystemMemoryGiB"] * 1024**3)
            elif isinstance(memory_summary.get("TotalSystemMemoryMiB"), (int, float)):
                memory_total = int(memory_summary["TotalSystemMemoryMiB"] * 1024**2)
            else:
                memories = self.collection_members(system.get("Memory"), warnings, "Memory")
                memory_total = sum(
                    int(item.get("CapacityBytes") or
                        (item.get("CapacityMiB", 0) * 1024**2))
                    for item in memories
                    if isinstance(item.get("CapacityBytes") or item.get("CapacityMiB"), (int, float))
                )
            if not memory_total:
                warnings.append("Total memory was not reported by the BMC.")

            disks = []
            storage_resources = self.collection_members(system.get("Storage"), warnings, "Storage")
            for storage in storage_resources:
                disks.extend(
                    self.collection_members(storage.get("Drives"), warnings, "Storage drives")
                )
            if not disks and system.get("Drives"):
                disks = self.collection_members(system.get("Drives"), warnings, "Drives")
            normalized_disks = []
            for index, disk in enumerate(disks):
                size = disk.get("CapacityBytes") or disk.get("SizeBytes") or 0
                normalized_disks.append(
                    {
                        "serial": disk.get("SerialNumber") or disk.get("Id") or f"drive-{index + 1}",
                        "model": disk.get("Model") or disk.get("Name") or "Unknown drive",
                        "size": int(size) if isinstance(size, (int, float)) else 0,
                    }
                )
            if not normalized_disks:
                warnings.append("No physical drives were reported by the BMC.")

            interfaces = self.collection_members(
                system.get("EthernetInterfaces"), warnings, "Ethernet interfaces"
            )
            normalized_interfaces = []
            for index, interface in enumerate(interfaces):
                mac = interface.get("MACAddress") or interface.get("PermanentMACAddress")
                if mac:
                    normalized_interfaces.append(
                        {
                            "name": interface.get("Name") or interface.get("Id") or f"nic-{index + 1}",
                            "mac": mac,
                            "link_status": interface.get("LinkStatus"),
                        }
                    )
            if not normalized_interfaces:
                warnings.append("No Ethernet interface MAC addresses were reported by the BMC.")

            system_uuid = system.get("UUID")
            try:
                node_uuid = str(uuid.UUID(str(system_uuid)))
            except (ValueError, TypeError, AttributeError):
                node_uuid = str(uuid.uuid4())
                warnings.append("BMC did not provide a valid system UUID; a local UUID was assigned.")
            serial_number = system.get("SerialNumber")
            if not isinstance(serial_number, str):
                serial_number = ""
            if not serial_number:
                warnings.append("BMC did not report a system serial number.")

            inventory = {
                "system": {
                    "uuid": node_uuid,
                    "serial_number": serial_number,
                    "manufacturer": system.get("Manufacturer", ""),
                    "model": system.get("Model", ""),
                },
                "system_vendor": {"serial_number": serial_number},
                "cpus": {"count": cpu_count or 0, "model": cpu_model},
                "memory": {"total": memory_total},
                "disks": normalized_disks,
                "interfaces": normalized_interfaces,
            }
            return {
                "uuid": node_uuid,
                "serial_number": serial_number,
                "inventory": inventory,
                "warnings": warnings,
            }
        finally:
            if self.session_url:
                try:
                    self.request("DELETE", self.session_url)
                except RedfishError:
                    pass
DEFAULT_BLUEPRINT = {
    "clusterName": "ocp-baremetal",
    "baseDomain": "example.com",
    "controlPlaneTopology": "HighlyAvailable",
    "mirrorRegistries": ["registry.airgap.internal:5000"],
    "caCertBundle": "",
    "machineNetworkCidr": "",
    "dnsServers": [],
    "ntpServers": [],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                uuid TEXT PRIMARY KEY,
                bmc_address TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                inventory_json TEXT NOT NULL,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                is_placeholder INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'ipa',
                first_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO app_settings (key, value_json) VALUES (?, ?)",
            ("blueprint", json.dumps(DEFAULT_BLUEPRINT)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO app_settings (key, value_json) VALUES (?, ?)",
            ("redfish_endpoints", json.dumps(DEFAULT_REDFISH_ENDPOINTS)),
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(nodes)")
        }
        if "serial_number" not in columns:
            connection.execute(
                "ALTER TABLE nodes ADD COLUMN serial_number TEXT NOT NULL DEFAULT ''"
            )
        if "is_placeholder" not in columns:
            connection.execute(
                "ALTER TABLE nodes ADD COLUMN is_placeholder INTEGER NOT NULL DEFAULT 0"
            )
        if "source" not in columns:
            connection.execute(
                "ALTER TABLE nodes ADD COLUMN source TEXT NOT NULL DEFAULT 'ipa'"
            )


def normalized_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A valid node UUID is required.") from None


def validate_output_directory(destination):
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("An absolute Ansible output directory is required.")
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise ValueError("Ansible output directory must be an absolute path.")
    working_root = ANSIBLE_WORKING_DIR.resolve()
    destination_path = destination_path.resolve()
    try:
        destination_path.relative_to(working_root)
    except ValueError:
        raise ValueError(
            f"Output directory must be inside the configured Ansible workspace "
            f"({working_root})."
        ) from None
    return destination_path, working_root


def parse_inspection(payload, path_uuid=None):
    if not isinstance(payload, dict):
        raise ValueError("Inspection report must be a JSON object.")

    payload_uuid = payload.get("uuid") or payload.get("node_uuid")
    node_uuid = normalized_uuid(path_uuid or payload_uuid)
    if path_uuid and payload_uuid and normalized_uuid(payload_uuid) != node_uuid:
        raise ValueError("The report UUID does not match the URL UUID.")

    inventory = payload.get("inventory")
    if not isinstance(inventory, dict):
        raise ValueError("Inspection report must contain an inventory object.")

    bmc_address = payload.get("bmc_address") or inventory.get("bmc_address") or ""
    if not isinstance(bmc_address, str):
        raise ValueError("bmc_address must be a string.")

    system_vendor = inventory.get("system_vendor")
    system_serial = inventory.get("system")
    serial_number = (
        payload.get("serial_number")
        or inventory.get("serial_number")
        or (system_vendor.get("serial_number") if isinstance(system_vendor, dict) else None)
        or (system_vendor.get("serial") if isinstance(system_vendor, dict) else None)
        or (system_serial.get("serial_number") if isinstance(system_serial, dict) else None)
        or ""
    )
    if not isinstance(serial_number, str):
        raise ValueError("Node serial number must be a string.")

    return node_uuid, bmc_address, serial_number.strip(), inventory


def safe_ansible_name(value, field, node_uuid):
    if not isinstance(value, str) or not ANSIBLE_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"Node {node_uuid} needs a valid {field} (letters, numbers, '.', '_' or '-')."
        )
    return value


def validate_unique_ansible_hosts(preferences_by_uuid):
    seen_hosts = set()
    for node_uuid, preferences in preferences_by_uuid.items():
        host = preferences.get("ansibleHost")
        if not host:
            continue
        host = safe_ansible_name(host, "Ansible host name", node_uuid)
        if host in seen_hosts:
            raise ValueError(f"Duplicate Ansible host name: {host}.")
        seen_hosts.add(host)





def yaml_scalar(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    raise ValueError(f"Unsupported YAML value: {type(value).__name__}")


def yaml_dump(value, indent=0):
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return prefix + "{}"
        lines = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("YAML mapping keys must be strings.")
            key_text = json.dumps(key, ensure_ascii=False)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{prefix}{key_text}:")
                lines.append(yaml_dump(item, indent + 2))
            elif isinstance(item, dict):
                lines.append(f"{prefix}{key_text}: {{}}")
            elif isinstance(item, list):
                lines.append(f"{prefix}{key_text}: []")
            else:
                lines.append(f"{prefix}{key_text}: {yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return prefix + "[]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{prefix}-")
                lines.append(yaml_dump(item, indent + 2))
            elif isinstance(item, dict):
                lines.append(f"{prefix}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{prefix}- []")
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return prefix + yaml_scalar(value)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "OcpMetalUI/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.") from None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body must be between 1 byte and 5 MiB.")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must be valid JSON.") from None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/health":
            self.send_json(200, {"status": "ok"})
            return
        if path == "/api/v1/config":
            with connect_database() as connection:
                settings = {
                    row["key"]: json.loads(row["value_json"])
                    for row in connection.execute(
                        "SELECT key, value_json FROM app_settings "
                        "WHERE key IN ('blueprint', 'redfish_endpoints', 'output_directory')"
                    )
                }
            self.send_json(
                200,
                {
                    "ansibleWorkingDir": str(ANSIBLE_WORKING_DIR.resolve()),
                    "defaultOutputDirectory": str(ANSIBLE_WORKING_DIR.resolve()),
                    "outputDirectory": settings.get(
                        "output_directory", str(ANSIBLE_WORKING_DIR.resolve())
                    ),
                    "blueprint": {**DEFAULT_BLUEPRINT, **settings["blueprint"]},
                    "redfishEndpoints": settings["redfish_endpoints"],
                    "httpsEnabled": bool(TLS_CERT_FILE and TLS_KEY_FILE),
                },
            )
            return
        if path == "/api/v1/nodes":
            with connect_database() as connection:
                rows = connection.execute(
                    "SELECT uuid, bmc_address, serial_number, inventory_json, preferences_json, "
                    "is_placeholder, source, "
                    "first_seen, updated_at FROM nodes ORDER BY uuid"
                ).fetchall()
            self.send_json(
                200,
                [
                    {
                        "uuid": row["uuid"],
                        "bmc_address": row["bmc_address"],
                        "serial_number": row["serial_number"],
                        "inventory": json.loads(row["inventory_json"]),
                        "preferences": json.loads(row["preferences_json"]),
                        "is_placeholder": bool(row["is_placeholder"]),
                        "source": row["source"],
                        "first_seen": row["first_seen"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ],
            )
            return
        self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/v1/redfish/retrieve" and not isinstance(
            self.connection, ssl.SSLSocket
        ):
            self.send_json(
                426,
                {
                    "error": "Redfish credentials are accepted only over HTTPS. "
                    "Configure the UI certificate and key before retrieving BMC inventory."
                },
            )
            return
        try:
            payload = self.read_json()
            if path == "/api/v1/inspections":
                self.store_inspection(payload)
            elif re.fullmatch(r"/api/v1/inspections/[^/]+", path):
                path_uuid = unquote(path.rsplit("/", 1)[1])
                self.store_inspection(payload, path_uuid)
            elif path == "/api/v1/ansible-files":
                self.commit_ansible_files(payload)
            elif path == "/api/v1/nodes":
                self.create_named_node(payload)
            elif path == "/api/v1/nodes/link":
                self.link_named_node(payload)
            elif path == "/api/v1/nodes/assignments":
                self.assign_discovered_nodes(payload)
            elif path == "/api/v1/redfish/retrieve":
                self.retrieve_redfish_endpoint(payload)
            else:
                self.send_json(404, {"error": "Not found."})
        except RedfishError as error:
            self.send_json(502, {"error": str(error)})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"error": "SQLite operation failed."})
        except OSError as error:
            self.send_json(500, {"error": f"Unable to commit Ansible files: {error}"})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/v1/configuration":
            try:
                self.save_configuration(self.read_json())
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except sqlite3.Error:
                self.send_json(500, {"error": "SQLite operation failed."})
            return
        if path == "/api/v1/redfish/endpoints":
            try:
                self.save_redfish_endpoints(self.read_json())
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except sqlite3.Error:
                self.send_json(500, {"error": "SQLite operation failed."})
            return
        if path != "/api/v1/nodes/preferences":
            self.send_json(404, {"error": "Not found."})
            return
        try:
            payload = self.read_json()
            preferences = payload.get("preferences") if isinstance(payload, dict) else None
            if not isinstance(preferences, dict) or not preferences:
                raise ValueError("Request must contain a non-empty preferences object.")
            normalized = {}
            for node_uuid, value in preferences.items():
                node_uuid = normalized_uuid(node_uuid)
                if not isinstance(value, dict):
                    raise ValueError(f"Preferences for {node_uuid} must be an object.")
                unknown_fields = set(value) - PREFERENCE_FIELDS
                if unknown_fields:
                    raise ValueError(
                        f"Unsupported preference field(s): {', '.join(sorted(unknown_fields))}"
                    )
                normalized[node_uuid] = json.dumps(value, ensure_ascii=False)

            with connect_database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = {
                    row["uuid"]: json.loads(row["preferences_json"])
                    for row in connection.execute("SELECT uuid, preferences_json FROM nodes")
                }
                existing = rows.keys()
                missing = set(normalized) - existing
                if missing:
                    raise ValueError(f"Unknown node UUID(s): {', '.join(sorted(missing))}")
                for node_uuid, value in normalized.items():
                    rows[node_uuid].update(json.loads(value))
                validate_unique_ansible_hosts(rows)
                for node_uuid, value in normalized.items():
                    connection.execute(
                        "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                        (value, utc_now(), node_uuid),
                    )
            self.send_json(200, {"saved": len(normalized)})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"error": "SQLite operation failed."})

    def do_DELETE(self):
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/v1/nodes/([^/]+)", path)
        if not match:
            self.send_json(404, {"error": "Not found."})
            return
        try:
            node_uuid = normalized_uuid(unquote(match.group(1)))
            with connect_database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                node = connection.execute(
                    "SELECT preferences_json, is_placeholder FROM nodes WHERE uuid = ?",
                    (node_uuid,),
                ).fetchone()
                if node is None:
                    raise ValueError("The selected node no longer exists.")
                host = json.loads(node["preferences_json"]).get("ansibleHost")
                if not node["is_placeholder"] and host:
                    existing_host = connection.execute(
                        "SELECT 1 FROM nodes WHERE uuid != ? AND "
                        "json_extract(preferences_json, '$.ansibleHost') = ? LIMIT 1",
                        (node_uuid, host),
                    ).fetchone()
                    if existing_host is None:
                        now = utc_now()
                        connection.execute(
                            """
                            INSERT INTO nodes
                                (uuid, bmc_address, serial_number, inventory_json, preferences_json,
                                 is_placeholder, source, first_seen, updated_at)
                            VALUES (?, '', '', '{}', ?, 1, 'manual', ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                json.dumps({"ansibleHost": host}),
                                now,
                                now,
                            ),
                        )
                cursor = connection.execute("DELETE FROM nodes WHERE uuid = ?", (node_uuid,))
                if cursor.rowcount == 0:
                    raise ValueError("The selected node no longer exists.")
            self.send_json(200, {"status": "deleted", "uuid": node_uuid})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"error": "SQLite operation failed."})

    def save_configuration(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must contain a configuration object.")
        blueprint = payload.get("blueprint")
        preferences_payload = payload.get("preferences")
        if not isinstance(blueprint, dict):
            raise ValueError("Request must contain a blueprint object.")
        if not isinstance(preferences_payload, dict):
            raise ValueError("Preferences must be an object keyed by node UUID.")
        unknown_blueprint_fields = set(blueprint) - BLUEPRINT_FIELDS
        if unknown_blueprint_fields:
            raise ValueError(
                f"Unsupported blueprint field(s): {', '.join(sorted(unknown_blueprint_fields))}"
            )
        destination_path, _ = validate_output_directory(
            payload.get("outputDirectory", str(ANSIBLE_WORKING_DIR.resolve()))
        )
        with connect_database() as connection:
            stored_blueprint = connection.execute(
                "SELECT value_json FROM app_settings WHERE key = 'blueprint'"
            ).fetchone()
        normalized_blueprint = {
            **DEFAULT_BLUEPRINT,
            **(json.loads(stored_blueprint["value_json"]) if stored_blueprint else {}),
            **blueprint,
        }
        for key in (
            "clusterName",
            "baseDomain",
            "controlPlaneTopology",
            "caCertBundle",
            "machineNetworkCidr",
        ):
            if not isinstance(normalized_blueprint[key], str):
                raise ValueError(f"Blueprint field {key} must be a string.")
        for key in ("mirrorRegistries", "dnsServers", "ntpServers"):
            if not isinstance(normalized_blueprint[key], list) or any(
                not isinstance(value, str) for value in normalized_blueprint[key]
            ):
                raise ValueError(f"Blueprint {key} must be a list of strings.")

        normalized_preferences = {}
        for node_uuid, preferences in preferences_payload.items():
            normalized_uuid_value = normalized_uuid(node_uuid)
            if not isinstance(preferences, dict):
                raise ValueError(f"Preferences for {normalized_uuid_value} must be an object.")
            unknown_fields = set(preferences) - PREFERENCE_FIELDS
            if unknown_fields:
                raise ValueError(
                    f"Unsupported preference field(s): {', '.join(sorted(unknown_fields))}"
                )
            normalized_preferences[normalized_uuid_value] = preferences

        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = {
                row["uuid"]: json.loads(row["preferences_json"])
                for row in connection.execute(
                    "SELECT uuid, preferences_json FROM nodes"
                )
            }
            missing = set(normalized_preferences) - rows.keys()
            if missing:
                raise ValueError(f"Unknown node UUID(s): {', '.join(sorted(missing))}")
            now = utc_now()
            for node_uuid, incoming_preferences in normalized_preferences.items():
                saved_preferences = rows[node_uuid]
                saved_preferences.update(incoming_preferences)
            validate_unique_ansible_hosts(rows)
            for node_uuid, incoming_preferences in normalized_preferences.items():
                saved_preferences = rows[node_uuid]
                connection.execute(
                    "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                    (json.dumps(saved_preferences, ensure_ascii=False), now, node_uuid),
                )
            connection.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('blueprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (json.dumps(normalized_blueprint, ensure_ascii=False),),
            )
            connection.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('output_directory', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (json.dumps(str(destination_path)),),
            )
        self.send_json(
            200,
            {"status": "saved", "savedNodes": len(normalized_preferences)},
        )

    def store_inspection(self, payload, path_uuid=None):
        node_uuid, bmc_address, serial_number, inventory = parse_inspection(payload, path_uuid)
        now = utc_now()
        with connect_database() as connection:
            connection.execute(
                """
                INSERT INTO nodes
                    (uuid, bmc_address, serial_number, inventory_json, source, first_seen, updated_at)
                VALUES (?, ?, ?, ?, 'ipa', ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    bmc_address = excluded.bmc_address,
                    serial_number = CASE
                        WHEN excluded.serial_number = '' THEN nodes.serial_number
                        ELSE excluded.serial_number
                    END,
                    inventory_json = excluded.inventory_json,
                    source = 'ipa',
                    updated_at = excluded.updated_at
                """,
                (
                    node_uuid,
                    bmc_address,
                    serial_number,
                    json.dumps(inventory, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.send_json(202, {"uuid": node_uuid, "status": "stored"})

    def create_named_node(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Node details must be a JSON object.")
        ansible_host = safe_ansible_name(payload.get("ansibleHost"), "Ansible host name", "new node")
        node_uuid = str(uuid.uuid4())
        now = utc_now()
        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_hosts = {
                json.loads(row["preferences_json"]).get("ansibleHost")
                for row in connection.execute("SELECT preferences_json FROM nodes")
            }
            if ansible_host in existing_hosts:
                raise ValueError(f"Duplicate Ansible host name: {ansible_host}.")
            connection.execute(
                """
                INSERT INTO nodes
                    (uuid, bmc_address, serial_number, inventory_json, preferences_json,
                     is_placeholder, source, first_seen, updated_at)
                VALUES (?, '', '', '{}', ?, 1, 'manual', ?, ?)
                """,
                (
                    node_uuid,
                    json.dumps({"ansibleHost": ansible_host}),
                    now,
                    now,
                ),
            )
        self.send_json(
            201,
            {
                "uuid": node_uuid,
                "bmc_address": "",
                "serial_number": "",
                "inventory": {},
                "preferences": {
                    "ansibleHost": ansible_host,
                },
                "is_placeholder": True,
                "source": "manual",
                "first_seen": now,
                "updated_at": now,
            },
        )

    def link_named_node(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Link details must be a JSON object.")
        placeholder_uuid = normalized_uuid(payload.get("placeholderUuid"))
        node_uuid = normalized_uuid(payload.get("nodeUuid"))
        if placeholder_uuid == node_uuid:
            raise ValueError("A named host cannot be linked to itself.")

        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholder = connection.execute(
                "SELECT preferences_json, is_placeholder FROM nodes WHERE uuid = ?",
                (placeholder_uuid,),
            ).fetchone()
            discovered = connection.execute(
                "SELECT preferences_json, is_placeholder FROM nodes WHERE uuid = ?",
                (node_uuid,),
            ).fetchone()
            if placeholder is None or not placeholder["is_placeholder"]:
                raise ValueError("The selected named host no longer exists.")
            if discovered is None or discovered["is_placeholder"]:
                raise ValueError("The selected IPA node no longer exists.")

            named_preferences = json.loads(placeholder["preferences_json"])
            node_preferences = json.loads(discovered["preferences_json"])
            host = safe_ansible_name(
                named_preferences.get("ansibleHost"), "Ansible host name", node_uuid
            )
            for row in connection.execute(
                "SELECT uuid, preferences_json FROM nodes WHERE uuid NOT IN (?, ?)",
                (placeholder_uuid, node_uuid),
            ):
                if json.loads(row["preferences_json"]).get("ansibleHost") == host:
                    raise ValueError(f"Duplicate Ansible host name: {host}.")
            node_preferences["ansibleHost"] = host
            previous_host = json.loads(discovered["preferences_json"]).get("ansibleHost")
            if previous_host and previous_host != host:
                old_host_exists = connection.execute(
                    "SELECT 1 FROM nodes WHERE uuid NOT IN (?, ?) AND "
                    "json_extract(preferences_json, '$.ansibleHost') = ? LIMIT 1",
                    (placeholder_uuid, node_uuid, previous_host),
                ).fetchone()
                if old_host_exists is None:
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO nodes
                            (uuid, bmc_address, serial_number, inventory_json, preferences_json,
                             is_placeholder, source, first_seen, updated_at)
                        VALUES (?, '', '', '{}', ?, 1, 'manual', ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            json.dumps({"ansibleHost": previous_host}),
                            now,
                            now,
                        ),
                    )
            connection.execute(
                "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                (json.dumps(node_preferences), utc_now(), node_uuid),
            )
            connection.execute("DELETE FROM nodes WHERE uuid = ?", (placeholder_uuid,))

        self.send_json(
            200,
            {
                "status": "linked",
                "placeholderUuid": placeholder_uuid,
                "nodeUuid": node_uuid,
            },
        )

    def assign_discovered_nodes(self, payload):
        assignments = payload.get("assignments") if isinstance(payload, dict) else None
        if not isinstance(assignments, list):
            raise ValueError("Assignments must be provided as a list.")

        normalized_assignments = []
        seen_nodes = set()
        seen_placeholders = set()
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise ValueError("Each discovered-node assignment must be an object.")
            node_uuid = normalized_uuid(assignment.get("nodeUuid"))
            if node_uuid in seen_nodes:
                raise ValueError(f"Node {node_uuid} is assigned more than once.")
            seen_nodes.add(node_uuid)

            placeholder_value = assignment.get("placeholderUuid")
            host_value = assignment.get("ansibleHost")
            if assignment.get("unlink") is True and not placeholder_value and not host_value:
                normalized_assignments.append({"nodeUuid": node_uuid, "unlink": True})
            elif placeholder_value and not host_value:
                placeholder_uuid = normalized_uuid(placeholder_value)
                if placeholder_uuid in seen_placeholders:
                    raise ValueError("An inventory item cannot be assigned to multiple nodes.")
                seen_placeholders.add(placeholder_uuid)
                normalized_assignments.append(
                    {"nodeUuid": node_uuid, "placeholderUuid": placeholder_uuid}
                )
            elif host_value and not placeholder_value:
                host = safe_ansible_name(host_value, "Ansible host name", node_uuid)
                normalized_assignments.append({"nodeUuid": node_uuid, "ansibleHost": host})
            else:
                raise ValueError(f"Node {node_uuid} must have exactly one valid assignment.")

        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = {
                row["uuid"]: row
                for row in connection.execute(
                    "SELECT uuid, preferences_json, is_placeholder FROM nodes"
                )
            }
            consumed_placeholders = {
                assignment["placeholderUuid"]
                for assignment in normalized_assignments
                if "placeholderUuid" in assignment
            }
            target_node_ids = {assignment["nodeUuid"] for assignment in normalized_assignments}
            occupied_hosts = set()
            for row in rows.values():
                if row["uuid"] in consumed_placeholders or row["uuid"] in target_node_ids:
                    continue
                preferences = json.loads(row["preferences_json"])
                if preferences.get("ansibleHost"):
                    occupied_hosts.add(preferences["ansibleHost"])

            requested_hosts = set()
            resolved_assignments = {}
            for assignment in normalized_assignments:
                node = rows.get(assignment["nodeUuid"])
                if node is None or node["is_placeholder"]:
                    raise ValueError("The selected discovered node no longer exists.")
                preferences = json.loads(node["preferences_json"])

                if "placeholderUuid" in assignment:
                    placeholder = rows.get(assignment["placeholderUuid"])
                    if placeholder is None or not placeholder["is_placeholder"]:
                        raise ValueError("The selected inventory item no longer exists.")
                    placeholder_preferences = json.loads(placeholder["preferences_json"])
                    host = placeholder_preferences.get("ansibleHost")
                    if not host:
                        raise ValueError("The selected inventory item has no Ansible host name.")
                    preferences = {**placeholder_preferences, **preferences}
                elif assignment.get("unlink"):
                    host = None
                else:
                    host = assignment["ansibleHost"]

                if host and (host in occupied_hosts or host in requested_hosts):
                    raise ValueError(f"Duplicate Ansible host name: {host}.")
                if host:
                    requested_hosts.add(host)
                    preferences["ansibleHost"] = host
                else:
                    preferences.pop("ansibleHost", None)
                resolved_assignments[assignment["nodeUuid"]] = (preferences, host)

            now = utc_now()
            for node_uuid, (preferences, _) in resolved_assignments.items():
                connection.execute(
                    "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                    (json.dumps(preferences, ensure_ascii=False), now, node_uuid),
                )
            for placeholder_uuid in consumed_placeholders:
                connection.execute("DELETE FROM nodes WHERE uuid = ?", (placeholder_uuid,))

            restored_hosts = {
                json.loads(rows[node_uuid]["preferences_json"]).get("ansibleHost")
                for node_uuid in target_node_ids
            }
            restored_hosts.discard(None)
            for host in sorted(restored_hosts - requested_hosts - occupied_hosts):
                placeholder_uuid = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO nodes
                        (uuid, bmc_address, serial_number, inventory_json, preferences_json,
                         is_placeholder, source, first_seen, updated_at)
                    VALUES (?, '', '', '{}', ?, 1, 'manual', ?, ?)
                    """,
                    (
                        placeholder_uuid,
                        json.dumps({"ansibleHost": host}),
                        now,
                        now,
                    ),
                )

        self.send_json(200, {"updated": len(resolved_assignments)})

    def save_redfish_endpoints(self, payload):
        endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
        if not isinstance(endpoints, list):
            raise ValueError("Redfish endpoints must be provided as a list.")
        validated = []
        seen_ids = set()
        for index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, dict):
                raise ValueError(f"Redfish endpoint {index + 1} must be an object.")
            endpoint_id = endpoint.get("id")
            name = endpoint.get("name")
            if not isinstance(endpoint_id, str) or not endpoint_id.strip():
                raise ValueError(f"Redfish endpoint {index + 1} needs an id.")
            if endpoint_id in seen_ids:
                raise ValueError(f"Duplicate Redfish endpoint id: {endpoint_id}.")
            seen_ids.add(endpoint_id)
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ValueError(f"Redfish endpoint {index + 1} needs a name (up to 80 characters).")
            validated.append(
                {
                    "id": endpoint_id,
                    "name": name.strip(),
                    "baseUrl": validate_redfish_url(endpoint.get("baseUrl")),
                }
            )
        with connect_database() as connection:
            connection.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('redfish_endpoints', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (json.dumps(validated, ensure_ascii=False),),
            )
        self.send_json(200, {"endpoints": validated})

    def retrieve_redfish_endpoint(self, payload):
        if not isinstance(self.connection, ssl.SSLSocket):
            self.send_json(
                426,
                {
                    "error": "Redfish credentials are accepted only over HTTPS. "
                    "Configure the UI certificate and key before retrieving BMC inventory."
                },
            )
            return
        if not isinstance(payload, dict):
            raise ValueError("Redfish retrieval details must be a JSON object.")
        endpoint_id = payload.get("endpointId")
        username = payload.get("username")
        password = payload.get("password")
        allow_untrusted_tls = payload.get("allowUntrustedTls")
        if not isinstance(endpoint_id, str):
            raise ValueError("A saved Redfish endpoint must be selected.")
        if not isinstance(username, str) or not username:
            raise ValueError("Enter the BMC username.")
        if not isinstance(password, str) or not password:
            raise ValueError("Enter the BMC password.")
        if not isinstance(allow_untrusted_tls, bool):
            raise ValueError("Confirm whether to accept an untrusted BMC certificate.")

        with connect_database() as connection:
            setting = connection.execute(
                "SELECT value_json FROM app_settings WHERE key = 'redfish_endpoints'"
            ).fetchone()
        endpoints = json.loads(setting["value_json"]) if setting else []
        endpoint = next((item for item in endpoints if item["id"] == endpoint_id), None)
        if endpoint is None:
            raise ValueError("The selected Redfish endpoint no longer exists.")

        try:
            report = RedfishClient(
                endpoint["baseUrl"], allow_untrusted_tls=allow_untrusted_tls
            ).retrieve(username, password)
        except RedfishError as error:
            safe_message = str(error).replace(username, "[redacted]")
            safe_message = safe_message.replace(password, "[redacted]")
            raise RedfishError(safe_message, status=error.status) from None
        node_uuid = normalized_uuid(report["uuid"])
        preferences = {}
        now = utc_now()
        with connect_database() as connection:
            existing = connection.execute(
                "SELECT preferences_json FROM nodes WHERE uuid = ?", (node_uuid,)
            ).fetchone()
            if existing:
                current_preferences = json.loads(existing["preferences_json"])
                current_preferences.update(preferences)
                preferences = current_preferences
            connection.execute(
                """
                INSERT INTO nodes
                    (uuid, bmc_address, serial_number, inventory_json, preferences_json,
                     is_placeholder, source, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, 'redfish', ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    bmc_address = excluded.bmc_address,
                    serial_number = CASE
                        WHEN excluded.serial_number = '' THEN nodes.serial_number
                        ELSE excluded.serial_number
                    END,
                    inventory_json = excluded.inventory_json,
                    preferences_json = excluded.preferences_json,
                    is_placeholder = 0,
                    source = 'redfish',
                    updated_at = excluded.updated_at
                """,
                (
                    node_uuid,
                    endpoint["baseUrl"],
                    report["serial_number"],
                    json.dumps(report["inventory"], ensure_ascii=False),
                    json.dumps(preferences, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.send_json(
            200,
            {
                "uuid": node_uuid,
                "serial_number": report["serial_number"],
                "source": "redfish",
                "warnings": report["warnings"],
                "inventory": report["inventory"],
                "credentialsStored": False,
                "untrustedTlsAccepted": allow_untrusted_tls,
            },
        )

    def build_ansible_files(self, payload):
        blueprint, inventory_groups, host_vars = self.prepare_ansible_files(payload)
        files = {
            "inventory.yml": self.build_inventory(inventory_groups),
        }
        for group in sorted(inventory_groups):
            files[f"group_vars/{group}.yml"] = yaml_dump(
                {"cluster_blueprint": blueprint}
            ) + "\n"
        for hostname, variables in sorted(host_vars.items()):
            files[f"host_vars/{hostname}.yml"] = yaml_dump(variables) + "\n"
        return files

    def prepare_ansible_files(self, payload):
        blueprint = payload.get("blueprint") if isinstance(payload, dict) else None
        if not isinstance(blueprint, dict):
            raise ValueError("Request must contain a blueprint object.")
        blueprint = {
            **DEFAULT_BLUEPRINT,
            **{key: value for key, value in blueprint.items() if key in BLUEPRINT_FIELDS},
        }
        with connect_database() as connection:
            rows = connection.execute(
                "SELECT uuid, bmc_address, serial_number, inventory_json, preferences_json, "
                "is_placeholder, source "
                "FROM nodes ORDER BY uuid"
            ).fetchall()
        inventory_groups = {}
        host_vars = {}
        seen_hosts = set()
        seen_serials = set()
        preferences_payload = payload.get("preferences", {})
        if not isinstance(preferences_payload, dict):
            raise ValueError("Preferences must be an object keyed by node UUID.")
        for row in rows:
            node_uuid = row["uuid"]
            serial_number = row["serial_number"]
            is_placeholder = bool(row["is_placeholder"])
            preferences = json.loads(row["preferences_json"])
            incoming_preferences = preferences_payload.get(node_uuid, {})
            if not isinstance(incoming_preferences, dict):
                raise ValueError(f"Preferences for {node_uuid} must be an object.")
            unknown_fields = set(incoming_preferences) - PREFERENCE_FIELDS
            if unknown_fields:
                raise ValueError(
                    f"Unsupported preference field(s): {', '.join(sorted(unknown_fields))}"
                )
            preferences.update(incoming_preferences)
            if not is_placeholder and not serial_number:
                source_name = "Redfish BMC" if row["source"] == "redfish" else "IPA report"
                raise ValueError(
                    f"Node {node_uuid} has no serial number in its {source_name} inventory."
                )
            if serial_number and serial_number in seen_serials:
                raise ValueError(f"Duplicate node serial number: {serial_number}.")
            if serial_number:
                seen_serials.add(serial_number)
            ansible_host = safe_ansible_name(
                preferences.get("ansibleHost"), "Ansible host name", node_uuid
            )
            ansible_group = DEFAULT_ANSIBLE_GROUP
            if ansible_host in seen_hosts:
                raise ValueError(f"Duplicate Ansible host name: {ansible_host}.")
            seen_hosts.add(ansible_host)

            host_entry = {}
            if serial_number:
                host_entry["serial_number"] = serial_number
            if row["bmc_address"]:
                address_key = (
                    "bmc_address" if row["source"] == "redfish" else "ansible_host"
                )
                host_entry[address_key] = row["bmc_address"]
            inventory_groups.setdefault(ansible_group, {})[ansible_host] = host_entry
            variables = {
                "node_preferences": {
                    key: value
                    for key, value in preferences.items()
                    if key in PREFERENCE_FIELDS and key != "ansibleHost"
                },
            }
            if is_placeholder:
                variables["node_status"] = "awaiting_ipa"
            else:
                variables.update(
                    {
                        "serial_number": serial_number,
                        "bmc_address": row["bmc_address"],
                        "inventory_source": row["source"],
                    }
                )
                variables[
                    "redfish_uuid" if row["source"] == "redfish" else "ironic_uuid"
                ] = node_uuid
                inventory_key = (
                    "redfish_inventory" if row["source"] == "redfish" else "ipa_inventory"
                )
                variables[inventory_key] = json.loads(row["inventory_json"])
            host_vars[ansible_host] = variables

        return blueprint, inventory_groups, host_vars

    def build_inventory(self, inventory_groups):
        inventory = {
            "all": {
                "children": {
                    group: {"hosts": hosts}
                    for group, hosts in sorted(inventory_groups.items())
                }
            }
        }
        return yaml_dump(inventory) + "\n"

    def commit_ansible_files(self, payload):
        destination = payload.get("outputDirectory") if isinstance(payload, dict) else None
        destination_path, working_root = validate_output_directory(destination)

        preferences_payload = payload.get("preferences", {})
        if not isinstance(preferences_payload, dict):
            raise ValueError("Preferences must be an object keyed by node UUID.")
        normalized_preferences = {}
        for node_uuid, preferences in preferences_payload.items():
            node_uuid = normalized_uuid(node_uuid)
            if not isinstance(preferences, dict):
                raise ValueError(f"Preferences for {node_uuid} must be an object.")
            normalized_preferences[node_uuid] = json.dumps(preferences, ensure_ascii=False)

        with connect_database() as connection:
            existing = {
                row["uuid"]
                for row in connection.execute("SELECT uuid FROM nodes")
            }
        missing = set(normalized_preferences) - existing
        if missing:
            raise ValueError(f"Unknown node UUID(s): {', '.join(sorted(missing))}")

        files = self.build_ansible_files(payload)
        blueprint = {
            **DEFAULT_BLUEPRINT,
            **{
                key: value
                for key, value in payload["blueprint"].items()
                if key in BLUEPRINT_FIELDS
            },
        }
        output_candidate = destination_path / ANSIBLE_OUTPUT_SUBDIR
        if output_candidate.is_symlink():
            raise ValueError("Generated output directory cannot be a symbolic link.")
        output_root = output_candidate.resolve()
        try:
            output_root.relative_to(working_root)
        except ValueError:
            raise ValueError("Generated output directory resolves outside the Ansible workspace.") from None

        output_root.mkdir(parents=True, exist_ok=True)
        for relative_path, content in files.items():
            target = output_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.resolve().relative_to(output_root)
            except ValueError:
                raise ValueError(f"Generated file path escapes output directory: {relative_path}") from None
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, delete=False
            ) as temporary_file:
                temporary_file.write(content)
                temporary_path = Path(temporary_file.name)
            try:
                temporary_path.replace(target)
            finally:
                temporary_path.unlink(missing_ok=True)

        with connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for node_uuid, serialized in normalized_preferences.items():
                connection.execute(
                    "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                    (serialized, utc_now(), node_uuid),
                )
            connection.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('blueprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (json.dumps(blueprint, ensure_ascii=False),),
            )
            connection.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('output_directory', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (json.dumps(str(destination_path)),),
            )
        self.send_json(
            200,
            {
                "status": "ready",
                "outputDirectory": str(output_root),
                "files": sorted(files),
                "inventoryFile": str(output_root / "inventory.yml"),
            },
        )

    def serve_static(self, request_path):
        relative_path = unquote(request_path).lstrip("/")
        candidate = (DIST_DIR / relative_path).resolve()
        try:
            candidate.relative_to(DIST_DIR.resolve())
        except ValueError:
            self.send_error(404)
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            candidate = DIST_DIR / "index.html"
        if not candidate.is_file():
            self.send_error(503, "Web assets are not built. Run npm run build.")
            return
        content_type = {
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".ico": "image/x-icon",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.umask(0o002)
    initialize_database()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    if bool(TLS_CERT_FILE) != bool(TLS_KEY_FILE):
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE must be configured together.")
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    protocol = "HTTP"
    if TLS_CERT_FILE and TLS_KEY_FILE:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
        httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
        protocol = "HTTPS"
    print(
        f"Serving OCP Metal UI on {protocol} {host}:{port}; "
        f"SQLite database: {DATABASE_PATH}"
    )
    httpd.serve_forever()
