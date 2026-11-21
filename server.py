import hashlib
import hmac
import html
import importlib.util
import json
import http.client
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from binascii import hexlify, unhexlify

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import psycopg
from psycopg.rows import dict_row


APP_DIR = Path(__file__).resolve().parent
DIST_DIR = APP_DIR / "dist"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ANSIBLE_WORKING_DIR = Path(os.environ.get("ANSIBLE_WORKING_DIR", "/ansible"))
# The image build copies the operator-supplied vault here. The default stays
# on /app so a checkout is not read by unit tests. pull-secret.vault is not
# stored in git. The vault password file is mounted at run time and is not
# in the image.
BUNDLED_PULL_SECRET_FILE = Path(
    os.environ.get("BUNDLED_PULL_SECRET_FILE", "/app/pull-secret.vault")
)
VAULT_PASSWORD_FILE = Path(os.environ.get("VAULT_PASSWORD_FILE", "/vault/password"))
TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE")
TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE")
MAX_REQUEST_BYTES = 5 * 1024 * 1024
# Ironic-style inspection reports are usually tens or hundreds of kilobytes.
# Two mebibytes covers a report with many disks, NICs, and LLDP neighbors.
INSPECTION_MAX_BYTES = 2 * 1024 * 1024
# Extra revisions are stored, then the oldest rows for that node are pruned.
# A report is never rejected because of this limit.
INSPECTION_REPORT_RETENTION = 20
# Intake does not take the UI advisory lock. A row lock waits this long, then
# the write is retried, so a UI update cannot stall a report forever.
_INTAKE_LOCK_TIMEOUT = "2s"
_INTAKE_WRITE_ATTEMPTS = 3
_INTAKE_RETRYABLE = (
    psycopg.errors.SerializationFailure,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.LockNotAvailable,
)
# Chunked trailers are not part of the report. Cap them so a client cannot
# hold the socket open after the terminating chunk.
_CHUNK_TRAILER_MAX_LINES = 16
_CHUNK_TRAILER_MAX_BYTES = 8 * 1024
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
    "ipv4LastOctet",
    "role",
    "roles",
    "rendezvous",
}
DEFAULT_ANSIBLE_GROUP = "ocp"
BLUEPRINT_FIELDS = {
    "clusterName",
    "baseDomain",
    "controlPlaneTopology",
    "mirrorRegistries",
    "caCertBundle",
    "machineNetworkCidr",
    "dnsServers",
    "ntpServers",
    "genesisHostname",
    "genesisIp",
    "genesisNic",
    "genesisNics",
    "genesisDisks",
    "networkGateway",
    "mgmtNetworkCidr",
    "mgmtVlan",
    "mgmtDc01Octet",
    "mgmtDc02Octet",
    "apiVip",
    "ingressVip",
}
# These stay on the blueprint so the form can reload them. They are host
# vars for dc01 and dc02, so they are not written to group_vars/ocp.yml.
_DC_OCTET_FIELDS = {
    "mgmtDc01Octet",
    "mgmtDc02Octet",
}
_MGMT_HOSTS = (
    ("dc01", "mgmtDc01Octet"),
    ("dc02", "mgmtDc02Octet"),
)
_MGMT_GROUP = "mgmt"
_HOST_LEVEL_PREFERENCES = {"ansibleHost", "ipv4LastOctet", "role", "roles", "rendezvous"}
_OPTIONAL_BLANK_GROUP_VARS = {"apiVip", "ingressVip"}
DEFAULT_REDFISH_ENDPOINTS = []


class RedfishError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class InstallSecretError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class InspectionRequestError(Exception):
    def __init__(self, status, message):
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
    "baseDomain": "ocp.internal",
    "controlPlaneTopology": "Compact",
    "mirrorRegistries": ["registry.airgap.internal:5000"],
    "caCertBundle": "",
    "machineNetworkCidr": "192.0.2.0/24",
    "dnsServers": ["192.0.2.35"],
    "ntpServers": [],
    "genesisHostname": "edge",
    "genesisIp": "192.0.2.35",
    "genesisNic": "eno5",
    "genesisNics": ["eno5"],
    "genesisDisks": [],
    "networkGateway": "192.0.2.1",
    "mgmtNetworkCidr": "198.51.100.0/24",
    "mgmtVlan": "280",
    "mgmtDc01Octet": "6",
    "mgmtDc02Octet": "7",
    "apiVip": "",
    "ingressVip": "",
}
# Saved blueprints from earlier builds used these names. The values move
# onto genesisHostname and genesisIp, and the old keys are not written again.
_RETIRED_BLUEPRINT_FIELDS = {
    "bastionHostname": "genesisHostname",
    "bastionIp": "genesisIp",
}
# Full DC addresses used to live in the group file. The last octet is now
# a host var. A stored address moves onto the octet key, then the address
# key is dropped.
_RETIRED_DC_ADDRESS_FIELDS = {
    "mgmtDc01Ip": "mgmtDc01Octet",
    "mgmtDc02Ip": "mgmtDc02Octet",
}


def bare_octet(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text.isdigit():
        return None
    number = int(text)
    if number > 255:
        return None
    return str(number)


def octet_from_address(value):
    if isinstance(value, str) and value.count(".") == 3:
        return bare_octet(value.strip().split(".")[-1])
    return bare_octet(value)


def optional_octet(value, label):
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    octet = bare_octet(value)
    if octet is None:
        raise ValueError(f"{label} must be a last octet from 0 to 255.")
    return octet


def optional_ipv4(value, label):
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an IPv4 address.")
    parts = value.strip().split(".")
    if len(parts) != 4 or any(not part.isdigit() or int(part) > 255 for part in parts):
        raise ValueError(f"{label} must be an IPv4 address.")
    return ".".join(str(int(part)) for part in parts)


def canonicalize_vips(blueprint):
    canonical = dict(blueprint)
    for key in ("apiVip", "ingressVip"):
        canonical[key] = optional_ipv4(canonical.get(key, ""), f"Blueprint field {key}")
    return canonical


_ROLE_ORDER = ("master", "worker", "storage")
_TOPOLOGIES = {"Compact", "HighlyAvailable", "SingleNode"}


def normalize_topology(value):
    if value not in _TOPOLOGIES:
        raise ValueError("Topology must be Compact, HighlyAvailable, or SingleNode.")
    return value


def normalize_role(value, node_uuid):
    if value is None or value == "":
        return ""
    if value not in {"master", "worker"}:
        raise ValueError(f"Node {node_uuid} role must be master or worker.")
    return value


def normalize_roles(value, node_uuid):
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(f"Node {node_uuid} roles must be a list.")
    selected = []
    for item in items:
        if not isinstance(item, str) or item not in _ROLE_ORDER:
            raise ValueError(
                f"Node {node_uuid} role must be master, worker, or storage."
            )
        if item not in selected:
            selected.append(item)
    return [name for name in _ROLE_ORDER if name in selected]


def installer_role(roles):
    if "master" in roles:
        return "master"
    if "worker" in roles:
        return "worker"
    return ""


def apply_role_fields(preferences, node_uuid):
    """Keep the role list and the installer role together.

    roles is the operator's selection and can hold more than one duty.
    role is the single value the agent installer accepts: master wins
    over worker. Storage stays in the list.
    """
    if "roles" in preferences:
        roles = normalize_roles(preferences.get("roles"), node_uuid)
        preferences["roles"] = roles
        preferences["role"] = installer_role(roles)
    elif "role" in preferences:
        role = normalize_role(preferences.get("role"), node_uuid)
        preferences["role"] = role
        preferences["roles"] = [role] if role else []
    return preferences


def normalize_rendezvous(value, node_uuid):
    if value is None or value == "":
        return False
    if not isinstance(value, bool):
        raise ValueError(f"Node {node_uuid} rendezvous must be true or false.")
    return value


def validate_node_roles(preferences_by_uuid):
    rendezvous_hosts = []
    for node_uuid, preferences in preferences_by_uuid.items():
        if "roles" in preferences or "role" in preferences:
            apply_role_fields(preferences, node_uuid)
        if "rendezvous" in preferences:
            preferences["rendezvous"] = normalize_rendezvous(
                preferences.get("rendezvous"), node_uuid
            )
        if preferences.get("rendezvous"):
            roles = preferences.get("roles") or []
            if "master" not in roles and preferences.get("role") != "master":
                raise ValueError(f"Node {node_uuid} rendezvous host must be a master.")
            rendezvous_hosts.append(node_uuid)
    if len(rendezvous_hosts) > 1:
        raise ValueError("Only one node can be the rendezvous host.")


def published_ipv4(octet, cidr, current_ip):
    network = ""
    if isinstance(cidr, str) and "/" in cidr:
        network = cidr.split("/", 1)[0].strip()
    parts = network.split(".")
    if octet and len(parts) == 4 and all(part.isdigit() and int(part) <= 255 for part in parts):
        return ".".join(parts[:-1] + [str(int(octet))])
    if isinstance(current_ip, str):
        text = current_ip.strip().split("/", 1)[0]
        ip_parts = text.split(".")
        if len(ip_parts) == 4 and all(part.isdigit() and int(part) <= 255 for part in ip_parts):
            return ".".join(str(int(part)) for part in ip_parts)
    return ""


def migrate_dc_addresses(blueprint, strict=False):
    migrated = dict(blueprint)
    for old_key, new_key in _RETIRED_DC_ADDRESS_FIELDS.items():
        if old_key not in migrated:
            continue
        if new_key not in migrated:
            octet = octet_from_address(migrated[old_key])
            if octet is None:
                if strict:
                    raise ValueError(
                        f"{old_key} is not an IPv4 address. Put the last octet in {new_key}."
                    )
            else:
                migrated[new_key] = octet
        migrated.pop(old_key, None)
    for key in _DC_OCTET_FIELDS:
        if key not in migrated:
            continue
        octet = bare_octet(migrated[key])
        if octet is not None:
            migrated[key] = octet
    return migrated


def migrate_blueprint(blueprint):
    if not isinstance(blueprint, dict):
        return {}
    migrated = dict(blueprint)
    for old_key, new_key in _RETIRED_BLUEPRINT_FIELDS.items():
        if new_key not in migrated and old_key in migrated:
            migrated[new_key] = migrated[old_key]
        migrated.pop(old_key, None)
    return migrate_dc_addresses(migrated)


def canonicalize_dc_octets(blueprint):
    canonical = dict(blueprint)
    for key in _DC_OCTET_FIELDS:
        octet = bare_octet(canonical.get(key))
        if octet is None:
            raise ValueError(f"Blueprint field {key} must be a last octet from 0 to 255.")
        canonical[key] = octet
    return canonical


def device_names(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Blueprint {label} must be a list of strings.")
    names = []
    seen = set()
    for item in value:
        name = item.strip()
        if not name:
            continue
        if any(character.isspace() for character in name):
            raise ValueError(f"{label} entries are device names, one per line.")
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def canonicalize_genesis_devices(blueprint):
    canonical = dict(blueprint)
    nics = device_names(canonical.get("genesisNics", []), "genesisNics")
    service_nic = canonical.get("genesisNic")
    if isinstance(service_nic, str):
        service_nic = service_nic.strip()
        if service_nic and not any(character.isspace() for character in service_nic):
            if service_nic not in nics:
                nics.insert(0, service_nic)
    canonical["genesisNics"] = nics
    canonical["genesisDisks"] = device_names(
        canonical.get("genesisDisks", []), "genesisDisks"
    )
    return canonical


def blueprint_for_group_vars(incoming):
    migrated = migrate_blueprint(incoming)
    canonical = canonicalize_vips(
        canonicalize_genesis_devices(
            canonicalize_dc_octets(
                {
                    **DEFAULT_BLUEPRINT,
                    **{key: value for key, value in migrated.items() if key in BLUEPRINT_FIELDS},
                }
            )
        )
    )
    canonical["controlPlaneTopology"] = normalize_topology(
        canonical.get("controlPlaneTopology")
    )
    return canonical


def group_vars_document(blueprint):
    document = {}
    for key, value in blueprint.items():
        if key in _DC_OCTET_FIELDS:
            continue
        if key in _OPTIONAL_BLANK_GROUP_VARS and isinstance(value, str) and not value.strip():
            continue
        document[key] = value
    return document


# A saved pull secret and SSH public key fill the review preview. Until
# those files exist, the preview keeps the placeholder. Neither value is
# written into inventory, group_vars, host_vars, or the database.
_PREVIEW_SUPPLIED_LATER = "<supplied at install>"
_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./+-]*")
_PLAIN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_NUMERIC_SCALAR = re.compile(r"-?\d+(?:\.\d+)?")
_RESERVED_SCALAR = {"true", "false", "null", "yes", "no", "on", "off", "~"}
_agent_hosts_filter = None
_agent_hosts_checked = False


def load_agent_hosts():
    global _agent_hosts_filter, _agent_hosts_checked
    if _agent_hosts_checked:
        return _agent_hosts_filter
    _agent_hosts_checked = True
    candidates = [APP_DIR / "agent_install.py"]
    override = os.environ.get("AGENT_INSTALL_FILTER")
    if override:
        candidates.insert(0, Path(override))
    candidates.append(
        APP_DIR.parent
        / "git-repos"
        / "ansible"
        / "ocp-ansible-playbooks"
        / "playbooks"
        / "filter_plugins"
        / "agent_install.py"
    )
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("ui_agent_install", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agent_hosts = getattr(module, "agent_hosts", None)
        if callable(agent_hosts):
            _agent_hosts_filter = agent_hosts
            break
    return _agent_hosts_filter


def preview_key(key):
    if _PLAIN_KEY.fullmatch(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def preview_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if (
            value
            and "\n" not in value
            and _PLAIN_SCALAR.fullmatch(value)
            and value.lower() not in _RESERVED_SCALAR
            and not _NUMERIC_SCALAR.fullmatch(value)
        ):
            return value
        return json.dumps(value, ensure_ascii=False)
    raise ValueError(f"Unsupported preview value: {type(value).__name__}")


def preview_lines(value, indent=0):
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            rendered_key = preview_key(key)
            if isinstance(item, dict):
                if item:
                    lines.append(f"{prefix}{rendered_key}:")
                    lines.extend(preview_lines(item, indent + 2))
                else:
                    lines.append(f"{prefix}{rendered_key}: {{}}")
            elif isinstance(item, list):
                if item:
                    lines.append(f"{prefix}{rendered_key}:")
                    lines.extend(preview_lines(item, indent))
                else:
                    lines.append(f"{prefix}{rendered_key}: []")
            else:
                lines.append(f"{prefix}{rendered_key}: {preview_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                pairs = list(item.items())
                first_key, first_value = pairs[0]
                first_key_text = preview_key(first_key)
                if isinstance(first_value, (dict, list)) and first_value:
                    lines.append(f"{prefix}- {first_key_text}:")
                    lines.extend(preview_lines(first_value, indent + 2))
                elif isinstance(first_value, dict):
                    lines.append(f"{prefix}- {first_key_text}: {{}}")
                elif isinstance(first_value, list):
                    lines.append(f"{prefix}- {first_key_text}: []")
                else:
                    lines.append(f"{prefix}- {first_key_text}: {preview_scalar(first_value)}")
                for key, item_value in pairs[1:]:
                    key_text = preview_key(key)
                    nested_prefix = prefix + "  "
                    if isinstance(item_value, (dict, list)) and item_value:
                        lines.append(f"{nested_prefix}{key_text}:")
                        child_indent = indent + 4 if isinstance(item_value, dict) else indent + 2
                        lines.extend(preview_lines(item_value, child_indent))
                    elif isinstance(item_value, dict):
                        lines.append(f"{nested_prefix}{key_text}: {{}}")
                    elif isinstance(item_value, list):
                        lines.append(f"{nested_prefix}{key_text}: []")
                    else:
                        lines.append(f"{nested_prefix}{key_text}: {preview_scalar(item_value)}")
            elif isinstance(item, list) and item:
                lines.append(f"{prefix}-")
                lines.extend(preview_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {preview_scalar(item)}")
        return lines
    return [prefix + preview_scalar(value)]


def preview_yaml(value):
    return "\n".join(preview_lines(value)) + "\n"


def render_install_document(result, pull_secret="", ssh_key=""):
    document = {
        "additionalTrustBundlePolicy": "Proxyonly",
        "apiVersion": "v1",
        "baseDomain": result["base_domain"],
        "fips": True,
        "imageDigestSources": [
            {
                "mirrors": [f"{result['edge_fqdn']}/ocp/openshift/release"],
                "source": "quay.io/openshift-release-dev/ocp-v4.0-art-dev",
            },
            {
                "mirrors": [f"{result['edge_fqdn']}/ocp/openshift/release-images"],
                "source": "quay.io/openshift-release-dev/ocp-release",
            },
        ],
        "compute": [
            {
                "architecture": "amd64",
                "hyperthreading": "Enabled",
                "name": "worker",
                "replicas": result["worker_replicas"],
            }
        ],
        "controlPlane": {
            "architecture": "amd64",
            "hyperthreading": "Enabled",
            "name": "master",
            "replicas": result["master_replicas"],
        },
        "metadata": {
            "creationTimestamp": None,
            "name": result["cluster_name"],
        },
        "networking": {
            "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
            "machineNetwork": [{"cidr": result["machine_network_cidr"]}],
            "networkType": "OVNKubernetes",
            "serviceNetwork": ["172.30.0.0/16"],
        },
    }
    if result["platform"] == "none":
        document["platform"] = {"none": {}}
    else:
        document["platform"] = {
            "baremetal": {
                "apiVIPs": list(result["api_vips"]),
                "ingressVIPs": list(result["ingress_vips"]),
            }
        }
    document["publish"] = "External"
    document["pullSecret"] = pull_secret or _PREVIEW_SUPPLIED_LATER
    document["sshKey"] = ssh_key or _PREVIEW_SUPPLIED_LATER
    ca_bundle = result.get("ca_cert_bundle")
    if isinstance(ca_bundle, str) and ca_bundle.strip():
        document["additionalTrustBundle"] = ca_bundle
    return document


def render_agent_document(result):
    document = {
        "apiVersion": "v1beta1",
        "kind": "AgentConfig",
        "metadata": {"name": result["cluster_name"]},
        "rendezvousIP": result["rendezvous_ip"],
    }
    if result["ntp_sources"]:
        document["additionalNTPSources"] = list(result["ntp_sources"])
    document["hosts"] = [
        {
            "hostname": host["hostname"],
            "role": host["role"],
            "interfaces": [
                {"name": iface["name"], "macAddress": iface["macAddress"]}
                for iface in host["interfaces"]
            ],
            "rootDeviceHints": {
                "serialNumber": host["root_device_hints"]["serialNumber"]
            },
            "networkConfig": host["network_config"],
        }
        for host in result["hosts"]
    ]
    return document


def config_preview(blueprint, inventory_groups, host_vars, pull_secret="", ssh_key=""):
    empty = {"installConfig": "", "agentConfig": "", "configPreviewError": ""}
    agent_hosts = load_agent_hosts()
    if agent_hosts is None:
        empty["configPreviewError"] = "Install preview is unavailable."
        return empty
    ocp_hosts = inventory_groups.get(DEFAULT_ANSIBLE_GROUP) or {}
    if not ocp_hosts:
        empty["configPreviewError"] = "The inventory has no hosts in the ocp group."
        return empty
    try:
        result = agent_hosts(
            {
                "blueprint": group_vars_document(blueprint),
                "hosts": [
                    {"name": name, "variables": host_vars[name]}
                    for name in ocp_hosts
                ],
            }
        )
    except Exception:
        empty["configPreviewError"] = "Install preview failed."
        return empty
    if not result.get("ok"):
        empty["configPreviewError"] = result.get("error") or "Install preview failed."
        return empty
    empty["installConfig"] = preview_yaml(
        render_install_document(result, pull_secret=pull_secret, ssh_key=ssh_key)
    )
    empty["agentConfig"] = preview_yaml(render_agent_document(result))
    return empty


DEFAULT_NODE_PREFERENCES = {
    "netType": "single",
    "singleNic": "eno5",
    "singleVlanEnabled": True,
    "singleVlanId": "281",
    "bondVlanEnabled": True,
    "bondVlanId": "281",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# One lock for every request, so overlapping requests cannot interleave
# a read-then-update of the same nodes.
_UI_DB_LOCK = 8342751
_JSON_EXTRACT = re.compile(
    r"json_extract\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'\$\.([A-Za-z_][A-Za-z0-9_]*)'\s*\)"
)


def translate_sql(sql):
    if sql.strip() == "BEGIN IMMEDIATE":
        return None
    translated = _JSON_EXTRACT.sub(r"(\1::json ->> '\2')", sql)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", translated, re.IGNORECASE):
        translated = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\b",
            "INSERT",
            translated,
            count=1,
            flags=re.IGNORECASE,
        )
        translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated.replace("?", "%s")


class DatabaseConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        translated = translate_sql(sql)
        if translated is None:
            return self._connection.execute("SELECT 1")
        if params is None:
            return self._connection.execute(translated)
        return self._connection.execute(translated, params)


def database_log_target():
    if not DATABASE_URL:
        return "PostgreSQL (DATABASE_URL is not set)"
    parsed = urlparse(DATABASE_URL)
    host = parsed.hostname or "database"
    port = parsed.port or 5432
    name = parsed.path.lstrip("/").split("?", 1)[0] or "database"
    return f"PostgreSQL {host}:{port}/{name}"


@contextmanager
def connect_database(advisory_lock=True, lock_timeout=None):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required.")
    connection = psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
        row_factory=dict_row,
    )
    try:
        with connection:
            if advisory_lock:
                connection.execute("SET lock_timeout = '10s'")
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_UI_DB_LOCK,))
            elif lock_timeout:
                connection.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (lock_timeout,),
                )
            yield DatabaseConnection(connection)
    finally:
        connection.close()


def initialize_database():
    with connect_database() as connection:
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
            CREATE TABLE IF NOT EXISTS inspection_reports (
                id BIGSERIAL PRIMARY KEY,
                node_uuid TEXT NOT NULL,
                received_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS inspection_reports_node_uuid_idx
            ON inspection_reports (node_uuid, id)
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
            "INSERT INTO app_settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT (key) DO NOTHING",
            ("blueprint", json.dumps(DEFAULT_BLUEPRINT)),
        )
        connection.execute(
            "INSERT INTO app_settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT (key) DO NOTHING",
            ("redfish_endpoints", json.dumps(DEFAULT_REDFISH_ENDPOINTS)),
        )
        columns = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'nodes'
                """
            )
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


# The configurator install owns these trees. The UI writes its inventory
# and install files only in its own workspace.
RESERVED_OUTPUT_ROOTS = (
    Path("/var/ocp-config"),
    Path("/opt/ocp"),
    Path("/etc/ansible"),
)


def reserved_output_root(path):
    for root in RESERVED_OUTPUT_ROOTS:
        if path == root or root in path.parents:
            return root
    return None


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
    reserved = reserved_output_root(destination_path)
    if reserved is not None:
        raise ValueError(
            "Output directory is inside the configurator install "
            f"({reserved}). Choose a workspace outside /var/ocp-config, "
            "/opt/ocp, and /etc/ansible."
        )
    return destination_path, working_root


CLUSTER_INSTALL_DIRNAME = "cluster-install"
_PULL_SECRET_NAME = "pull-secret.json"
_PULL_SECRET_VAULT = "pull-secret.vault"
_VAULT_VARIABLE = "ocp_pre_install_pull_secret_contents"
_PRIVATE_KEY_NAME = "id_ed25519"
_PUBLIC_KEY_NAME = "id_ed25519.pub"
_MAX_PULL_SECRET_CHARS = 1024 * 1024
_MAX_VAULT_CHARS = 2 * 1024 * 1024
_VAULT_PASSWORD_MISSING = (
    "The vault password file is not available, so the pull secret cannot be saved."
)
_VAULT_PASSWORD_REJECTED = "The vault password did not open the pull secret."
_MAX_PUBLIC_KEY_CHARS = 16384
_MAX_PRIVATE_KEY_BYTES = 65536
_SSH_PUBLIC_KEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}(?: .*)?$"
)
# HMAC-SHA256 of 8-character fragments that must not appear in a pull secret.
# The fragments and an unsalted digest are not stored here.
_FRAGMENT_LENGTH = 8
_FRAGMENT_HMAC_KEY = bytes.fromhex(
    "5c3fed91e1880ab4777cd1b840ca8f669d524143357ca4b3c4320cce68dd8f8e"
)
_REJECTED_FRAGMENT_MACS = frozenset(
    {
        "1032ed170987a98fb2de470a51fb2d2e2a26ec5ecab07c61932d6af287d9f4fb",
        "c71e53c181317105baa6a9751d370884fa55d86dd4922196175599a8c31a9e70",
    }
)
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_INSTALL_SECRET_POSTS = {
    "/api/v1/install-secrets/pull-secret",
    "/api/v1/install-secrets/ssh-key",
    "/api/v1/install-secrets/ssh-public-key",
}
_PUBLIC_KEY_ERROR = (
    "Paste one public key line that starts with ssh-ed25519, ssh-rsa, "
    "or ecdsa-sha2-nistp256."
)
_PRIVATE_KEY_PASTE_ERROR = (
    "That looks like a private key. Paste the public key, one line that "
    "starts with ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp256."
)


def normalize_pull_secret(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            "Paste the cluster pull secret, or choose the pull-secret.json file you downloaded."
        )
    if len(text) > _MAX_PULL_SECRET_CHARS:
        raise ValueError("That pull secret is too large.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(
            "The pull secret must be the JSON you downloaded, with an auths object."
        ) from None
    auths = parsed.get("auths") if isinstance(parsed, dict) else None
    if not isinstance(auths, dict):
        raise ValueError("The pull secret must be JSON with an auths object.")
    if not auths:
        raise ValueError("The pull secret needs at least one registry under auths.")
    for name, entry in auths.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise ValueError("Each auths entry must be an object keyed by a registry name.")
    normalized = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    if pull_secret_contains_rejected_fragment(normalized):
        raise ValueError("That pull secret is not accepted.")
    return normalized


def pull_secret_contains_rejected_fragment(text):
    if not isinstance(text, str) or len(text) < _FRAGMENT_LENGTH:
        return False
    for index in range(len(text) - _FRAGMENT_LENGTH + 1):
        window = text[index:index + _FRAGMENT_LENGTH]
        encoded = window.encode("utf-8")
        if len(encoded) != _FRAGMENT_LENGTH:
            continue
        digest = hmac.new(_FRAGMENT_HMAC_KEY, encoded, hashlib.sha256).hexdigest()
        if digest in _REJECTED_FRAGMENT_MACS:
            return True
    return False


def _token_from_file(file_name):
    try:
        return Path(file_name).read_bytes().strip()
    except OSError:
        return None


def configured_admin_token():
    """Return the admin token, or None when it is missing.

    A configured token file that cannot be read fails closed. An empty
    result means protected routes must not run.
    """
    file_name = os.environ.get("API_TOKEN_FILE", "").strip()
    if file_name:
        token = _token_from_file(file_name)
        if not token:
            return None
        return token
    token = os.environ.get("API_TOKEN", "").encode("utf-8").strip()
    return token or None


def bearer_matches(header_value, expected):
    presented = b""
    if isinstance(header_value, str):
        parts = header_value.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            presented = parts[1].strip().encode("utf-8")
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented).digest(),
        hashlib.sha256(expected).digest(),
    )


def is_inspection_intake(path):
    """Exact intake routes. Case variants stay behind the admin token."""
    if path == "/api/v1/inspections":
        return True
    return re.fullmatch(r"/api/v1/inspections/[^/]+", path) is not None


def request_hostname(host_header):
    if not isinstance(host_header, str):
        return ""
    host = host_header.strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        if end < 2:
            return ""
        remainder = host[end + 1:]
        if remainder and not re.fullmatch(r":[0-9]+", remainder):
            return ""
        return host[1:end]
    if host.count(":") > 1:
        return ""
    if ":" in host:
        name, port = host.split(":", 1)
        if not port.isdigit():
            return ""
        return name.rstrip(".")
    return host.rstrip(".")


def normalize_allowed_name(value):
    name = value.strip().lower()
    if not name:
        return ""
    # [2001:db8::1] and [2001:db8::1]:443 are the same host as the bare address.
    if name.startswith("["):
        return request_hostname(name)
    return name.rstrip(".")


def allowed_api_hosts():
    names = {"localhost", "127.0.0.1", "::1"}
    bind = normalize_allowed_name(os.environ.get("HOST", "127.0.0.1"))
    if bind and bind not in {"0.0.0.0", "::"}:
        names.add(bind)
    for source in (
        os.environ.get("TLS_HOSTNAME", ""),
        os.environ.get("API_ALLOWED_HOSTS", ""),
    ):
        for item in source.split(","):
            name = normalize_allowed_name(item)
            if name:
                names.add(name)
    return names


def host_is_allowed(host_header):
    return request_hostname(host_header) in allowed_api_hosts()


def inspection_report_retention():
    raw = os.environ.get("INSPECTION_REPORT_RETENTION", "").strip()
    if not raw:
        return INSPECTION_REPORT_RETENTION
    try:
        value = int(raw)
    except ValueError:
        return INSPECTION_REPORT_RETENTION
    if value < 1:
        return INSPECTION_REPORT_RETENTION
    return value


def authorize_api(method, path, authorization):
    """Return None when the request may proceed, or (status, error).

    GET /api/v1/health and the two inspection intake posts are public.
    Every other /api/ path requires the admin token. A Bearer header on
    intake is ignored.
    """
    normalized_path = path.lower()
    if not normalized_path.startswith("/api/"):
        return None
    # Only the exact health path is public. /api/v1/HEALTH is not that route.
    if path == "/api/v1/health":
        return None
    # Hardware reports are unauthenticated intake. A Bearer header, including
    # a stale inspection token from an older collector, is ignored.
    if method == "POST" and is_inspection_intake(path):
        return None
    admin = configured_admin_token()
    if admin is None:
        return 503, "The API token is not configured."
    if bearer_matches(authorization, admin):
        return None
    return 401, "Authentication is required."


def normalize_ssh_public_key(text):
    if not isinstance(text, str):
        raise ValueError(_PUBLIC_KEY_ERROR)
    if "PRIVATE KEY" in text or "-----BEGIN" in text:
        raise ValueError(_PRIVATE_KEY_PASTE_ERROR)
    if len(text) > _MAX_PUBLIC_KEY_CHARS:
        raise ValueError(_PUBLIC_KEY_ERROR)
    line = text.strip()
    if "\n" in line or not _SSH_PUBLIC_KEY.fullmatch(line):
        raise ValueError(_PUBLIC_KEY_ERROR)
    return line


def install_secret_directory(destination):
    if isinstance(destination, Path):
        destination = str(destination)
    destination_path, working_root = validate_output_directory(destination)
    candidate = destination_path / CLUSTER_INSTALL_DIRNAME
    if candidate.is_symlink():
        raise ValueError("cluster-install cannot be a symbolic link.")
    directory = candidate.resolve()
    try:
        directory.relative_to(working_root)
    except ValueError:
        raise ValueError("cluster-install resolves outside the Ansible workspace.") from None
    return directory


def _read_text_file(path, limit):
    if not path.is_file() or path.is_symlink():
        return None
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def vault_password_bytes():
    path = VAULT_PASSWORD_FILE
    if not isinstance(path, Path):
        path = Path(path)
    if path.is_symlink() or not path.is_file():
        return b""
    try:
        return path.read_bytes().strip()
    except OSError:
        return b""


def _vault_keys(password, salt):
    # Ansible vault 1.1 derives a 32-byte AES key, a 32-byte HMAC key, and a 16-byte CTR nonce.
    derived = hashlib.pbkdf2_hmac("sha256", password, salt, 10000, dklen=80)
    return derived[:32], derived[32:64], derived[64:80]


def encrypt_vault_envelope(plaintext, password):
    if not isinstance(password, (bytes, bytearray)) or not password:
        raise ValueError(_VAULT_PASSWORD_MISSING)
    raw = plaintext.encode("utf-8")
    salt = os.urandom(32)
    key, mac_key, nonce = _vault_keys(bytes(password), salt)
    # PKCS7 is part of the Ansible vault format even though the cipher is CTR.
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    digest = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    inner = b"\n".join((hexlify(salt), hexlify(digest), hexlify(ciphertext)))
    outer = hexlify(inner).decode("ascii")
    lines = ["$ANSIBLE_VAULT;1.1;AES256"]
    lines.extend(outer[index:index + 80] for index in range(0, len(outer), 80))
    return "\n".join(lines) + "\n"


def decrypt_vault_envelope(envelope, password):
    if not isinstance(password, (bytes, bytearray)) or not password:
        raise ValueError(_VAULT_PASSWORD_MISSING)
    lines = [line.strip() for line in envelope.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("$ANSIBLE_VAULT;") or "AES256" not in lines[0]:
        raise ValueError("That file is not an Ansible vault secret.")
    try:
        inner = unhexlify("".join(lines[1:]))
        salt_hex, digest_hex, cipher_hex = inner.split(b"\n", 2)
        salt = unhexlify(salt_hex)
        ciphertext = unhexlify(cipher_hex)
        expected = unhexlify(digest_hex)
    except (ValueError, TypeError):
        raise ValueError("That file is not an Ansible vault secret.") from None
    key, mac_key, nonce = _vault_keys(bytes(password), salt)
    actual = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError(_VAULT_PASSWORD_REJECTED)
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise ValueError(_VAULT_PASSWORD_REJECTED) from None
    return raw.decode("utf-8")


def format_pull_secret_vault(plaintext, password):
    envelope = encrypt_vault_envelope(plaintext, password).rstrip("\n")
    indented = "\n".join(f"          {line}" for line in envelope.splitlines())
    return f"{_VAULT_VARIABLE}: !vault |\n{indented}\n"


def extract_vault_envelope(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("$ANSIBLE_VAULT;"):
            continue
        body = [stripped]
        for extra in lines[index + 1:]:
            piece = extra.strip()
            if not piece:
                continue
            if not re.fullmatch(r"[0-9a-fA-F]+", piece):
                break
            body.append(piece)
        return "\n".join(body) + "\n"
    raise ValueError("That file is not an Ansible vault secret.")


def _vault_text_present(path):
    text = _read_text_file(path, _MAX_VAULT_CHARS)
    return bool(text and "$ANSIBLE_VAULT;" in text)


def _open_pull_secret_text(text):
    if "$ANSIBLE_VAULT;" in text:
        try:
            opened = decrypt_vault_envelope(extract_vault_envelope(text), vault_password_bytes())
        except ValueError:
            return ""
        text = opened
    try:
        return normalize_pull_secret(text)
    except ValueError:
        return ""


def _read_pull_secret(path):
    text = _read_text_file(path, _MAX_PULL_SECRET_CHARS)
    if text is None or "$ANSIBLE_VAULT;" in text:
        return ""
    return _open_pull_secret_text(text)


def _read_vaulted_pull_secret(path):
    text = _read_text_file(path, _MAX_VAULT_CHARS)
    if text is None or "$ANSIBLE_VAULT;" not in text:
        return ""
    return _open_pull_secret_text(text)


def _read_public_key(path):
    text = _read_text_file(path, _MAX_PUBLIC_KEY_CHARS)
    if text is None:
        return ""
    try:
        return normalize_ssh_public_key(text)
    except ValueError:
        return ""


def _private_key_stored(path):
    if not path.is_file() or path.is_symlink():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return 0 < size <= _MAX_PRIVATE_KEY_BYTES


def bundled_pull_secret_path():
    path = BUNDLED_PULL_SECRET_FILE
    if not isinstance(path, Path):
        path = Path(path)
    return path


def bundled_pull_secret():
    return _read_vaulted_pull_secret(bundled_pull_secret_path())


def workspace_pull_secret(directory):
    # A vault file saved for this workspace replaces the image copy. A
    # plaintext pull-secret.json is still read when no vault file is present.
    return (
        _read_vaulted_pull_secret(directory / _PULL_SECRET_VAULT)
        or _read_pull_secret(directory / _PULL_SECRET_NAME)
    )


def pull_secret_is_vaulted(destination):
    paths = [bundled_pull_secret_path()]
    try:
        directory = install_secret_directory(destination)
    except ValueError:
        directory = None
    if directory is not None:
        paths.append(directory / _PULL_SECRET_VAULT)
    return any(_vault_text_present(path) for path in paths)


def _secret_status(directory):
    workspace_secret = workspace_pull_secret(directory)
    built_in_secret = bundled_pull_secret()
    return {
        "pullSecretStored": _vault_text_present(directory / _PULL_SECRET_VAULT) or bool(
            _read_pull_secret(directory / _PULL_SECRET_NAME)
        ),
        "pullSecretBuiltIn": _vault_text_present(bundled_pull_secret_path()),
        "pullSecretReady": bool(workspace_secret or built_in_secret),
        "vaultPasswordAvailable": bool(vault_password_bytes()),
        "privateKeyStored": _private_key_stored(directory / _PRIVATE_KEY_NAME),
        "sshPublicKey": _read_public_key(directory / _PUBLIC_KEY_NAME),
    }


def install_secret_status(destination):
    return _secret_status(install_secret_directory(destination))


def stored_install_material(destination):
    try:
        directory = install_secret_directory(destination)
    except ValueError:
        return "", ""
    # The image copy stays in the image. It is not written into the workspace.
    pull_secret = workspace_pull_secret(directory) or bundled_pull_secret()
    return pull_secret, _read_public_key(directory / _PUBLIC_KEY_NAME)


def write_secret_file(path, data, mode):
    if isinstance(data, str):
        data = data.encode("utf-8")
    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        raise InstallSecretError("Refusing to write through a symbolic link.")
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = None
    previous_umask = os.umask(0o077)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Could not finish writing the install secret.")
            view = view[written:]
        os.fchmod(fd, mode)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        os.chmod(path, mode)
    except InstallSecretError:
        raise
    except OSError as error:
        raise InstallSecretError("Could not store the install secret.") from error
    finally:
        os.umask(previous_umask)
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def save_pull_secret(destination, pull_secret):
    normalized = normalize_pull_secret(pull_secret)
    password = vault_password_bytes()
    if not password:
        raise ValueError(_VAULT_PASSWORD_MISSING)
    directory = install_secret_directory(destination)
    legacy = directory / _PULL_SECRET_NAME
    if legacy.is_symlink():
        raise ValueError("pull-secret.json cannot be a symbolic link.")
    vault_path = directory / _PULL_SECRET_VAULT
    if vault_path.is_symlink():
        raise ValueError("pull-secret.vault cannot be a symbolic link.")
    write_secret_file(
        vault_path,
        format_pull_secret_vault(normalized, password),
        0o600,
    )
    if legacy.is_file():
        legacy.unlink()
    return _secret_status(directory)


def save_ssh_public_key(destination, public_key, replace=False):
    if not isinstance(replace, bool):
        raise ValueError("replace must be true or false.")
    normalized = normalize_ssh_public_key(public_key)
    directory = install_secret_directory(destination)
    private_path = directory / _PRIVATE_KEY_NAME
    current = _read_public_key(directory / _PUBLIC_KEY_NAME)
    private_stored = _private_key_stored(private_path)
    if private_stored and current != normalized and not replace:
        raise InstallSecretError(
            "An SSH private key is already stored. Confirm replace to use a different "
            "public key. The stored private key will be removed.",
            status=409,
        )
    if private_stored and current != normalized and replace:
        try:
            private_path.unlink()
        except OSError as error:
            raise InstallSecretError("Could not store the install secret.") from error
    write_secret_file(directory / _PUBLIC_KEY_NAME, normalized + "\n", 0o644)
    return _secret_status(directory)


def generate_cluster_ssh_key(destination, replace=False):
    if not isinstance(replace, bool):
        raise ValueError("replace must be true or false.")
    directory = install_secret_directory(destination)
    private_path = directory / _PRIVATE_KEY_NAME
    if _private_key_stored(private_path) and not replace:
        raise InstallSecretError(
            "An SSH key is already stored. Confirm that you want to replace it.",
            status=409,
        )
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise InstallSecretError(
            "This UI cannot create an SSH key because ssh-keygen is not installed. "
            "Paste a public key instead.",
            status=503,
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        with tempfile.TemporaryDirectory(dir=directory) as temporary_name:
            temp_private = Path(temporary_name) / _PRIVATE_KEY_NAME
            completed = subprocess.run(
                [
                    keygen,
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "ocp",
                    "-f",
                    str(temp_private),
                ],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            temp_public = Path(str(temp_private) + ".pub")
            if completed.returncode != 0 or not temp_private.is_file() or not temp_public.is_file():
                raise InstallSecretError("Could not create an SSH key.", status=500)
            private_bytes = temp_private.read_bytes()
            public_text = normalize_ssh_public_key(temp_public.read_text(encoding="utf-8"))
            if b"PRIVATE KEY" not in private_bytes or len(private_bytes) > _MAX_PRIVATE_KEY_BYTES:
                raise InstallSecretError("Could not create an SSH key.", status=500)
            write_secret_file(private_path, private_bytes, 0o600)
            write_secret_file(directory / _PUBLIC_KEY_NAME, public_text + "\n", 0o644)
    except InstallSecretError:
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallSecretError("Could not create an SSH key.", status=500) from error
    return _secret_status(directory)


_INSTALL_CONFIG_NAME = "install-config.yaml"
_AGENT_CONFIG_NAME = "agent-config.yaml"
_INSTALL_FILES_NEED_SECRETS = (
    "Save a pull secret and an SSH public key on the Blueprint page. "
    "Generate again to write install-config.yaml and agent-config.yaml."
)
_INSTALL_FILES_NEED_PULL_SECRET = (
    "Paste a pull secret on the Blueprint page. "
    "Generate again to write install-config.yaml and agent-config.yaml."
)
_INSTALL_FILES_NEED_SSH = (
    "Save an SSH public key on the Blueprint page. "
    "Generate again to write install-config.yaml and agent-config.yaml."
)


def install_files_gap_message(pull_secret, ssh_key, destination):
    if not pull_secret and pull_secret_is_vaulted(destination):
        if vault_password_bytes():
            vault_text = "The vault password did not open the pull secret."
        else:
            vault_text = (
                "The pull secret is in the Ansible vault. "
                "Provide the vault password file."
            )
        if ssh_key:
            return (
                vault_text
                + " Generate again to write install-config.yaml and agent-config.yaml."
            )
        return (
            vault_text
            + " An SSH public key is also required."
            + " Generate again to write install-config.yaml and agent-config.yaml."
        )
    if not pull_secret and not ssh_key:
        return _INSTALL_FILES_NEED_SECRETS
    if not pull_secret:
        return _INSTALL_FILES_NEED_PULL_SECRET
    if not ssh_key:
        return _INSTALL_FILES_NEED_SSH
    return ""


def remove_install_configs(destination):
    try:
        directory = install_secret_directory(destination)
    except ValueError:
        return
    if not directory.is_dir():
        return
    for name in (_INSTALL_CONFIG_NAME, _AGENT_CONFIG_NAME):
        path = directory / name
        if path.is_symlink():
            raise ValueError(f"{name} cannot be a symbolic link.")
        if path.is_file():
            path.unlink()


def write_install_configs(destination, install_text, agent_text):
    directory = install_secret_directory(destination)
    install_path = directory / _INSTALL_CONFIG_NAME
    agent_path = directory / _AGENT_CONFIG_NAME
    write_secret_file(install_path, install_text, 0o600)
    try:
        write_secret_file(agent_path, agent_text, 0o644)
    except Exception:
        if install_path.is_file() and not install_path.is_symlink():
            install_path.unlink()
        raise
    return install_path, agent_path


def read_private_key(destination):
    directory = install_secret_directory(destination)
    path = directory / _PRIVATE_KEY_NAME
    if not _private_key_stored(path):
        raise InstallSecretError(
            "No private key is stored. Create one, or paste a public key you already have.",
            status=404,
        )
    try:
        data = path.read_bytes()
    except OSError as error:
        raise InstallSecretError("The stored private key could not be read.", status=500) from error
    if b"PRIVATE KEY" not in data:
        raise InstallSecretError("The stored private key could not be read.", status=500)
    return data


def install_secret_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("Request must contain an object.")
    replace = payload.get("replace", False)
    if not isinstance(replace, bool):
        raise ValueError("replace must be true or false.")
    return payload.get("outputDirectory"), replace


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

    bmc_address = optional_inspection_text(payload.get("bmc_address"), "bmc_address")
    if not bmc_address:
        bmc_address = optional_inspection_text(inventory.get("bmc_address"), "bmc_address")

    system_vendor = inventory.get("system_vendor")
    system_serial = inventory.get("system")
    serial_number = optional_inspection_text(
        payload.get("serial_number")
        or inventory.get("serial_number")
        or (system_vendor.get("serial_number") if isinstance(system_vendor, dict) else None)
        or (system_vendor.get("serial") if isinstance(system_vendor, dict) else None)
        or (system_serial.get("serial_number") if isinstance(system_serial, dict) else None),
        "Node serial number",
    )

    return node_uuid, bmc_address, serial_number, inventory


def optional_inspection_text(value, label):
    # JSON null and a missing field are blank. The upsert keeps the stored value.
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    return value.strip()


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


def remove_replaced_host_vars(output_root, generated_files):
    """Drop host var files from the previous generation.

    The UI owns host_vars/*.yml under the generated directory. A later
    generation is the current Ansible inventory, so a renamed or removed
    host must not leave its old file behind. Other files, including
    group_vars the operator added, stay in place.
    """
    directory = output_root / "host_vars"
    if not directory.is_dir():
        return
    for existing in directory.iterdir():
        if existing.is_symlink() or not existing.is_file() or existing.suffix != ".yml":
            continue
        relative = f"host_vars/{existing.name}"
        if relative in generated_files:
            continue
        try:
            existing.resolve().relative_to(output_root)
        except ValueError:
            continue
        existing.unlink()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "OcpMetalUI/1.0"
    # Bound every socket read, including a chunked body that never finishes.
    # A timed-out read raises TimeoutError; handle_one_request closes it.
    timeout = 60

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def add_security_headers(self):
        self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_error(self, code, message=None, explain=None):
        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = "???", "???"
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        self.log_error("code %d, message %s", code, message)
        self.send_response(code, message)
        self.send_header("Connection", "close")
        body = None
        if code >= 200 and code not in (204, 205, 304):
            content = self.error_message_format % {
                "code": code,
                "message": html.escape(message, quote=False),
                "explain": html.escape(explain, quote=False),
            }
            body = content.encode("UTF-8", "replace")
            self.send_header("Content-Type", self.error_content_type)
            self.send_header("Content-Length", str(len(body)))
        self.add_security_headers()
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 413:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_attachment(self, filename, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def discard_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if 0 < length <= MAX_REQUEST_BYTES:
            self.rfile.read(length)

    def guard_request(self, method):
        path = urlparse(self.path).path
        # Intake is exempt from the Host allowlist. The collector posts to
        # whichever address reaches this process, and a missed IP would drop
        # the report. Every other route, including the UI, still checks Host.
        if not (method == "POST" and is_inspection_intake(path)):
            host_values = self.headers.get_all("Host") or []
            if len(host_values) != 1 or not host_is_allowed(host_values[0]):
                if method in {"POST", "PUT", "PATCH"}:
                    self.discard_body()
                self.send_json(421, {"error": "Host is not allowed."})
                return False
        decision = authorize_api(method, path, self.headers.get("Authorization", ""))
        if decision is None:
            return True
        status, error = decision
        if method in {"POST", "PUT", "PATCH"}:
            self.discard_body()
        self.send_json(status, {"error": error})
        return False

    def query_value(self, name):
        values = parse_qs(urlparse(self.path).query).get(name) or []
        if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise ValueError(f"{name} is required.")
        return values[0]

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
        if not self.guard_request("GET"):
            return
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
                    "blueprint": canonicalize_vips(
                        canonicalize_genesis_devices(
                            {
                                **DEFAULT_BLUEPRINT,
                                **migrate_blueprint(settings["blueprint"]),
                            }
                        )
                    ),
                    "redfishEndpoints": settings["redfish_endpoints"],
                    "httpsEnabled": bool(TLS_CERT_FILE and TLS_KEY_FILE),
                },
            )
            return
        if path == "/api/v1/install-secrets":
            try:
                self.send_json(200, install_secret_status(self.query_value("outputDirectory")))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            return
        if path == "/api/v1/install-secrets/ssh-private-key":
            if not isinstance(self.connection, ssl.SSLSocket):
                self.send_json(
                    426,
                    {"error": "Pull secrets and SSH keys are accepted only over HTTPS."},
                )
                return
            try:
                self.send_attachment("id_ed25519", read_private_key(self.query_value("outputDirectory")))
            except InstallSecretError as error:
                self.send_json(error.status, {"error": str(error)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
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
        if not self.guard_request("POST"):
            return
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
        if path in _INSTALL_SECRET_POSTS and not isinstance(self.connection, ssl.SSLSocket):
            self.discard_body()
            self.send_json(
                426,
                {"error": "Pull secrets and SSH keys are accepted only over HTTPS."},
            )
            return
        if is_inspection_intake(path):
            try:
                self.accept_inspection(path)
            except InspectionRequestError as error:
                self.send_json(error.status, {"error": str(error)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except psycopg.errors.LockNotAvailable:
                self.send_json(503, {"error": "The inspection store is busy."})
            except psycopg.Error:
                self.send_json(500, {"error": "Database operation failed."})
            return
        try:
            payload = self.read_json()
            if path == "/api/v1/ansible-files":
                self.commit_ansible_files(payload)
            elif path == "/api/v1/nodes":
                self.create_named_node(payload)
            elif path == "/api/v1/nodes/link":
                self.link_named_node(payload)
            elif path == "/api/v1/nodes/assignments":
                self.assign_discovered_nodes(payload)
            elif path == "/api/v1/redfish/retrieve":
                self.retrieve_redfish_endpoint(payload)
            elif path == "/api/v1/install-secrets/pull-secret":
                if not isinstance(payload, dict):
                    raise ValueError("Request must contain an object.")
                self.send_json(
                    200,
                    save_pull_secret(payload.get("outputDirectory"), payload.get("pullSecret")),
                )
            elif path == "/api/v1/install-secrets/ssh-key":
                destination, replace = install_secret_request(payload)
                self.send_json(200, generate_cluster_ssh_key(destination, replace=replace))
            elif path == "/api/v1/install-secrets/ssh-public-key":
                destination, replace = install_secret_request(payload)
                self.send_json(
                    200,
                    save_ssh_public_key(
                        destination, payload.get("sshPublicKey"), replace=replace
                    ),
                )
            else:
                self.send_json(404, {"error": "Not found."})
        except InstallSecretError as error:
            self.send_json(error.status, {"error": str(error)})
        except RedfishError as error:
            self.send_json(502, {"error": str(error)})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except psycopg.Error:
            self.send_json(500, {"error": "Database operation failed."})
        except OSError as error:
            self.send_json(500, {"error": f"Unable to commit Ansible files: {error}"})

    def do_PUT(self):
        if not self.guard_request("PUT"):
            return
        path = urlparse(self.path).path
        if path == "/api/v1/configuration":
            try:
                self.save_configuration(self.read_json())
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except psycopg.Error:
                self.send_json(500, {"error": "Database operation failed."})
            return
        if path == "/api/v1/redfish/endpoints":
            try:
                self.save_redfish_endpoints(self.read_json())
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except psycopg.Error:
                self.send_json(500, {"error": "Database operation failed."})
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
                if "roles" in value or "role" in value:
                    apply_role_fields(value, node_uuid)
                if "rendezvous" in value:
                    value["rendezvous"] = normalize_rendezvous(value.get("rendezvous"), node_uuid)
                if "ipv4LastOctet" in value:
                    value["ipv4LastOctet"] = optional_octet(
                        value["ipv4LastOctet"], f"Last octet for {node_uuid}"
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
                validate_node_roles(rows)
                for node_uuid, value in normalized.items():
                    connection.execute(
                        "UPDATE nodes SET preferences_json = ?, updated_at = ? WHERE uuid = ?",
                        (value, utc_now(), node_uuid),
                    )
            self.send_json(200, {"saved": len(normalized)})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except psycopg.Error:
            self.send_json(500, {"error": "Database operation failed."})

    def do_DELETE(self):
        if not self.guard_request("DELETE"):
            return
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
        except psycopg.Error:
            self.send_json(500, {"error": "Database operation failed."})

    def save_configuration(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must contain a configuration object.")
        blueprint = payload.get("blueprint")
        preferences_payload = payload.get("preferences")
        if not isinstance(blueprint, dict):
            raise ValueError("Request must contain a blueprint object.")
        if not isinstance(preferences_payload, dict):
            raise ValueError("Preferences must be an object keyed by node UUID.")
        blueprint = migrate_dc_addresses(blueprint, strict=True)
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
            **migrate_blueprint(
                json.loads(stored_blueprint["value_json"]) if stored_blueprint else {}
            ),
            **blueprint,
        }
        normalized_blueprint = canonicalize_genesis_devices(
            canonicalize_dc_octets(normalized_blueprint)
        )
        for key in (
            "clusterName",
            "baseDomain",
            "controlPlaneTopology",
            "caCertBundle",
            "machineNetworkCidr",
            "genesisHostname",
            "genesisIp",
            "genesisNic",
            "networkGateway",
            "mgmtNetworkCidr",
            "mgmtVlan",
            "mgmtDc01Octet",
            "mgmtDc02Octet",
            "apiVip",
            "ingressVip",
        ):
            if not isinstance(normalized_blueprint[key], str):
                raise ValueError(f"Blueprint field {key} must be a string.")
        normalized_blueprint = canonicalize_vips(normalized_blueprint)
        normalized_blueprint["controlPlaneTopology"] = normalize_topology(
            normalized_blueprint.get("controlPlaneTopology")
        )
        for key in ("mirrorRegistries", "dnsServers", "ntpServers", "genesisNics", "genesisDisks"):
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
            if "ipv4LastOctet" in preferences:
                preferences["ipv4LastOctet"] = optional_octet(
                    preferences["ipv4LastOctet"],
                    f"Last octet for {normalized_uuid_value}",
                )
            if "roles" in preferences or "role" in preferences:
                apply_role_fields(preferences, normalized_uuid_value)
            if "rendezvous" in preferences:
                preferences["rendezvous"] = normalize_rendezvous(
                    preferences.get("rendezvous"), normalized_uuid_value
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
            validate_node_roles(rows)
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

    def read_exact(self, size):
        remaining = size
        chunks = []
        while remaining:
            piece = self.rfile.read(remaining)
            if not piece:
                raise InspectionRequestError(400, "Inspection report ended early.")
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    def read_chunked_inspection_body(self):
        # Content-Type is ignored. Chunked collectors are accepted up to the
        # same cap; only a body past that cap is rejected.
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(128)
            if not line:
                raise InspectionRequestError(400, "Inspection report must be a JSON object.")
            size_token = line.split(b";", 1)[0].strip()
            try:
                size = int(size_token, 16)
            except ValueError as error:
                raise InspectionRequestError(400, "Inspection report must be a JSON object.") from error
            if size < 0:
                raise InspectionRequestError(400, "Inspection report must be a JSON object.")
            if size == 0:
                self.read_chunk_trailers()
                break
            if size > INSPECTION_MAX_BYTES or total + size > INSPECTION_MAX_BYTES:
                raise InspectionRequestError(413, "Inspection report is too large.")
            chunks.append(self.read_exact(size))
            total += size
            delimiter = self.rfile.readline(8)
            if delimiter not in (b"\r\n", b"\n"):
                raise InspectionRequestError(400, "Inspection report must be a JSON object.")
        return b"".join(chunks)

    def read_chunk_trailers(self):
        lines = 0
        total = 0
        while True:
            trailer = self.rfile.readline(_CHUNK_TRAILER_MAX_BYTES + 1)
            if trailer in (b"\r\n", b"\n", b""):
                return
            lines += 1
            total += len(trailer)
            if (
                lines > _CHUNK_TRAILER_MAX_LINES
                or total > _CHUNK_TRAILER_MAX_BYTES
                or len(trailer) > _CHUNK_TRAILER_MAX_BYTES
                or not trailer.endswith(b"\n")
            ):
                raise InspectionRequestError(400, "Inspection report trailers are too large.")

    def read_inspection_body(self):
        # Any Content-Type is accepted. The body is parsed as JSON anyway.
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            raw = self.read_chunked_inspection_body()
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise InspectionRequestError(400, "Invalid Content-Length.") from error
            if length <= 0:
                raise InspectionRequestError(400, "Inspection report must be a JSON object.")
            if length > INSPECTION_MAX_BYTES:
                raise InspectionRequestError(413, "Inspection report is too large.")
            raw = self.read_exact(length)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InspectionRequestError(400, "Inspection report must be a JSON object.") from error
        return payload

    def accept_inspection(self, path):
        path_uuid = None
        if path != "/api/v1/inspections":
            path_uuid = unquote(path.rsplit("/", 1)[1])
        payload = self.read_inspection_body()
        node_uuid, bmc_address, serial_number, inventory = parse_inspection(payload, path_uuid)
        now = utc_now()
        report_json = json.dumps(payload, ensure_ascii=False)
        retention = inspection_report_retention()
        inventory_json = json.dumps(inventory, ensure_ascii=False)
        for attempt in range(_INTAKE_WRITE_ATTEMPTS):
            try:
                with connect_database(
                    advisory_lock=False, lock_timeout=_INTAKE_LOCK_TIMEOUT
                ) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO nodes
                            (uuid, bmc_address, serial_number, inventory_json, source, first_seen, updated_at)
                        VALUES (?, ?, ?, ?, 'ipa', ?, ?)
                        ON CONFLICT(uuid) DO UPDATE SET
                            bmc_address = CASE
                                WHEN excluded.bmc_address IS NULL OR excluded.bmc_address = ''
                                    THEN nodes.bmc_address
                                ELSE excluded.bmc_address
                            END,
                            serial_number = CASE
                                WHEN excluded.serial_number IS NULL OR excluded.serial_number = ''
                                    THEN nodes.serial_number
                                ELSE excluded.serial_number
                            END,
                            inventory_json = excluded.inventory_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            node_uuid,
                            bmc_address,
                            serial_number,
                            inventory_json,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO inspection_reports (node_uuid, received_at, report_json)
                        VALUES (?, ?, ?)
                        """,
                        (node_uuid, now, report_json),
                    )
                break
            except _INTAKE_RETRYABLE:
                if attempt + 1 == _INTAKE_WRITE_ATTEMPTS:
                    raise
                time.sleep(0.05 * (attempt + 1))
        self.prune_inspection_reports(node_uuid, retention)
        self.send_json(202, {"status": "accepted"})

    def prune_inspection_reports(self, node_uuid, retention):
        # The report is already committed. A prune failure must not drop it.
        try:
            with connect_database(
                advisory_lock=False, lock_timeout=_INTAKE_LOCK_TIMEOUT
            ) as connection:
                connection.execute(
                    """
                    DELETE FROM inspection_reports
                    WHERE node_uuid = ?
                      AND id NOT IN (
                          SELECT id FROM inspection_reports
                          WHERE node_uuid = ?
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (node_uuid, node_uuid, retention),
                )
        except psycopg.Error as error:
            self.log_error("Inspection report retention prune failed: %s", error)

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
        return self.ansible_file_map(blueprint, inventory_groups, host_vars)

    def ansible_file_map(self, blueprint, inventory_groups, host_vars):
        files = {
            "inventory.yml": self.build_inventory(inventory_groups),
        }
        # group_vars/ocp.yml is the file Ansible loads for the ocp group.
        # The management last octets are host vars, so they are left out.
        # The mgmt group does not get a copy of the cluster blueprint.
        if DEFAULT_ANSIBLE_GROUP in inventory_groups:
            files[f"group_vars/{DEFAULT_ANSIBLE_GROUP}.yml"] = (
                yaml_dump(group_vars_document(blueprint)) + "\n"
            )
        for hostname, variables in sorted(host_vars.items()):
            files[f"host_vars/{hostname}.yml"] = yaml_dump(variables) + "\n"
        return files

    def prepare_ansible_files(self, payload):
        blueprint = payload.get("blueprint") if isinstance(payload, dict) else None
        if not isinstance(blueprint, dict):
            raise ValueError("Request must contain a blueprint object.")
        blueprint = blueprint_for_group_vars(blueprint)
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
        merged_preferences = {}
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
            apply_role_fields(preferences, node_uuid)
            merged_preferences[node_uuid] = preferences
            node_defaults = dict(DEFAULT_NODE_PREFERENCES)
            genesis_nic = blueprint.get("genesisNic")
            if isinstance(genesis_nic, str) and genesis_nic.strip():
                node_defaults["singleNic"] = genesis_nic.strip()
            for key, value in node_defaults.items():
                preferences.setdefault(key, value)
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
                    if key in PREFERENCE_FIELDS and key not in _HOST_LEVEL_PREFERENCES
                },
            }
            roles = preferences.get("roles") or []
            if roles:
                variables["roles"] = roles
            role = preferences.get("role") or ""
            if role:
                variables["role"] = role
            if "rendezvous" in preferences:
                variables["rendezvous"] = normalize_rendezvous(
                    preferences.get("rendezvous"), node_uuid
                )
            octet = ""
            if "ipv4LastOctet" in preferences:
                octet = optional_octet(
                    preferences.get("ipv4LastOctet"),
                    f"Last octet for {ansible_host}",
                )
                if octet:
                    variables["ipv4LastOctet"] = octet
            current_ip = ""
            if not is_placeholder:
                inventory = json.loads(row["inventory_json"])
                if isinstance(inventory, dict):
                    current_ip = inventory.get("current_ip") or ""
            address = published_ipv4(octet, blueprint.get("machineNetworkCidr"), current_ip)
            if address:
                variables["ipv4_ip"] = address
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

        validate_node_roles(merged_preferences)

        for hostname, octet_field in _MGMT_HOSTS:
            if hostname in seen_hosts:
                raise ValueError(f"{hostname} is reserved for the management DNS host.")
            seen_hosts.add(hostname)
            inventory_groups.setdefault(_MGMT_GROUP, {})[hostname] = {}
            host_vars[hostname] = {"ipv4LastOctet": blueprint[octet_field]}

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
            if "roles" in preferences or "role" in preferences:
                apply_role_fields(preferences, node_uuid)
            normalized_preferences[node_uuid] = json.dumps(preferences, ensure_ascii=False)

        with connect_database() as connection:
            existing = {
                row["uuid"]
                for row in connection.execute("SELECT uuid FROM nodes")
            }
        missing = set(normalized_preferences) - existing
        if missing:
            raise ValueError(f"Unknown node UUID(s): {', '.join(sorted(missing))}")

        blueprint, inventory_groups, host_vars = self.prepare_ansible_files(payload)
        files = self.ansible_file_map(blueprint, inventory_groups, host_vars)
        pull_secret, ssh_key = stored_install_material(destination_path)
        try:
            preview = config_preview(
                blueprint,
                inventory_groups,
                host_vars,
                pull_secret=pull_secret,
                ssh_key=ssh_key,
            )
        except Exception:
            preview = {
                "installConfig": "",
                "agentConfig": "",
                "configPreviewError": "Install preview failed.",
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
        remove_replaced_host_vars(output_root, set(files))

        install_config_file = ""
        agent_config_file = ""
        install_files_message = ""
        ready_to_write = (
            not preview["configPreviewError"]
            and preview["installConfig"]
            and preview["agentConfig"]
            and _PREVIEW_SUPPLIED_LATER not in preview["installConfig"]
        )
        if ready_to_write and pull_secret and ssh_key:
            install_path, agent_path = write_install_configs(
                destination_path,
                preview["installConfig"],
                preview["agentConfig"],
            )
            install_config_file = str(install_path)
            agent_config_file = str(agent_path)
        else:
            remove_install_configs(destination_path)
            if not preview["configPreviewError"] and preview["installConfig"]:
                install_files_message = install_files_gap_message(
                    pull_secret, ssh_key, destination_path
                )

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
                "installConfig": preview["installConfig"],
                "agentConfig": preview["agentConfig"],
                "configPreviewError": preview["configPreviewError"],
                "installConfigFile": install_config_file,
                "agentConfigFile": agent_config_file,
                "installFilesMessage": install_files_message,
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
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(body)


def _loopback_bind(host):
    return host in {"127.0.0.1", "::1", "localhost"}


if __name__ == "__main__":
    initialize_database()
    port = int(os.environ.get("PORT", "3003"))
    host = os.environ.get("HOST", "127.0.0.1")
    if bool(TLS_CERT_FILE) != bool(TLS_KEY_FILE):
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE must be configured together.")
    if not _loopback_bind(host) and not (TLS_CERT_FILE and TLS_KEY_FILE):
        print(
            f"Warning: listening on {host} without TLS. "
            "Set TLS_CERT_FILE and TLS_KEY_FILE, or bind 127.0.0.1.",
            file=sys.stderr,
        )
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    protocol = "HTTP"
    if TLS_CERT_FILE and TLS_KEY_FILE:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
        httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
        protocol = "HTTPS"
    print(
        f"Serving OCP Metal UI on {protocol} {host}:{port}; "
        f"database: {database_log_target()}"
    )
    httpd.serve_forever()
