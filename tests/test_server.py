import hashlib
import hmac
import http.client
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlparse

import server


NODE_UUID = "8a4f91c2-38d1-4bda-912c-bbfa47821001"


def playbook_root():
    """Optional checkout of ocp-ansible-playbooks/playbooks.

    Comparisons against that tree run only when OCP_ANSIBLE_PLAYBOOKS
    points at a playbooks directory. CI does not have that checkout.
    """
    configured = os.environ.get("OCP_ANSIBLE_PLAYBOOKS", "").strip()
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_dir() else None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_url = server.DATABASE_URL
        self.original_ansible_working_dir = server.ANSIBLE_WORKING_DIR
        if not os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL is required to run the backend tests.")
        server.DATABASE_URL = os.environ["DATABASE_URL"]
        server.ANSIBLE_WORKING_DIR = Path(self.temp_dir.name) / "ansible"
        server.ANSIBLE_WORKING_DIR.mkdir()
        server.initialize_database()
        with server.connect_database() as connection:
            connection.execute("TRUNCATE TABLE nodes, app_settings")
        self.api_token = "test-api-token"
        self.inspection_token = "test-inspection-token"
        self._api_token_env = patch.dict(
            os.environ,
            {"API_TOKEN": self.api_token, "INSPECTION_TOKEN": self.inspection_token},
        )
        self._api_token_env.start()
        self.addCleanup(self._api_token_env.stop)
        server.initialize_database()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        server.DATABASE_URL = self.original_database_url
        server.ANSIBLE_WORKING_DIR = self.original_ansible_working_dir
        self.temp_dir.cleanup()

    def _vault_password(self, password=b"test-vault-password"):
        path = Path(self.temp_dir.name) / "vault-password"
        path.write_bytes(password if password.endswith(b"\n") else password + b"\n")
        path.chmod(0o600)
        patcher = patch.object(server, "VAULT_PASSWORD_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def request(self, method, path, payload=None, token="__default__", host=None, authorization="__default__"):
        connection = http.client.HTTPConnection(*self.httpd.server_address)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if authorization == "__default__":
            if token == "__default__":
                token = self.api_token
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
        elif authorization is not None:
            headers["Authorization"] = authorization
        if host is not None:
            headers["Host"] = host
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.read()
        content_type = response.getheader("Content-Type", "")
        connection.close()
        return response.status, result, content_type

    def report(self, **overrides):
        payload = {
            "uuid": NODE_UUID,
            "bmc_address": "192.0.2.21",
            "inventory": {
                "cpu": {"count": 32, "model_name": "Example CPU"},
                "memory": {"physical_mb": 65536},
                "system_vendor": {"serial_number": "NODE-SERIAL-01"},
                "disks": [{"name": "/dev/sda", "model": "Example SSD", "size": 500000000000}],
                "interfaces": [
                    {
                        "name": "eno1",
                        "mac_address": "52:54:00:fa:19:bc",
                        "state": "up",
                    }
                ],
            },
        }
        payload.update(overrides)
        return payload

    def database_bytes(self):
        with server.connect_database() as connection:
            chunks = []
            for table in ("nodes", "app_settings"):
                for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                    chunks.append(json.dumps(dict(row), sort_keys=True).encode())
            return b"\n".join(chunks)

    def test_inspection_is_read_from_the_database_and_preferences_survive_reports(self):
        status, _, _ = self.request("POST", "/api/v1/inspections", self.report())
        self.assertEqual(status, 202)
        status, body, _ = self.request(
            "PUT",
            "/api/v1/nodes/preferences",
            {"preferences": {NODE_UUID: {"rootDisk": "/dev/sda", "netType": "single"}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"saved": 1})

        updated_report = self.report(bmc_address="192.0.2.99")
        status, _, _ = self.request("POST", f"/api/v1/inspections/{NODE_UUID}", updated_report)
        self.assertEqual(status, 202)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        node = json.loads(body)[0]
        self.assertEqual(node["bmc_address"], "192.0.2.99")
        self.assertEqual(node["serial_number"], "NODE-SERIAL-01")
        self.assertEqual(node["inventory"]["cpu"]["model_name"], "Example CPU")
        self.assertEqual(node["inventory"]["interfaces"][0]["state"], "up")
        self.assertEqual(node["preferences"]["rootDisk"], "/dev/sda")

    def test_configuration_commit_saves_blueprint_and_preferences_without_exporting(self):
        self.request(
            "POST",
            "/api/v1/inspections",
            {"uuid": NODE_UUID, "inventory": {"disks": [], "interfaces": []}},
        )
        status, body, _ = self.request(
            "PUT",
            "/api/v1/configuration",
            {
                "blueprint": {
                    "clusterName": "airgap-cluster",
                    "baseDomain": "example.test",
                    "controlPlaneTopology": "HighlyAvailable",
                    "mirrorRegistries": ["registry.internal:5000"],
                    "caCertBundle": "trusted CA",
                    "machineNetworkCidr": "192.168.10.0/24",
                    "dnsServers": ["192.168.10.10", "192.168.10.11"],
                    "ntpServers": ["time1.example.test"],
                },
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR / "deployment"),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "control-01", "netType": "bond"}
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "saved", "savedNodes": 1})
        self.assertFalse((server.ANSIBLE_WORKING_DIR / "ocp-metal-ui").exists())

        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertEqual(config["blueprint"]["clusterName"], "airgap-cluster")
        self.assertEqual(config["blueprint"]["machineNetworkCidr"], "192.168.10.0/24")
        self.assertEqual(config["blueprint"]["dnsServers"], ["192.168.10.10", "192.168.10.11"])
        self.assertEqual(config["blueprint"]["ntpServers"], ["time1.example.test"])
        self.assertEqual(
            config["outputDirectory"],
            str(server.ANSIBLE_WORKING_DIR / "deployment"),
        )
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)[0]["preferences"],
            {"ansibleHost": "control-01", "netType": "bond"},
        )

    def test_reserved_output_roots_are_the_configurator_trees(self):
        self.assertEqual(
            server.RESERVED_OUTPUT_ROOTS,
            (
                Path("/var/ocp-config"),
                Path("/opt/ocp"),
                Path("/etc/ansible"),
            ),
        )

    def test_output_directory_rejects_configurator_trees(self):
        reserved = server.ANSIBLE_WORKING_DIR / "ocp-config"
        reserved.mkdir()
        profile = reserved / "edm-v100-D"
        profile.mkdir()
        original = server.RESERVED_OUTPUT_ROOTS
        server.RESERVED_OUTPUT_ROOTS = (reserved,)
        try:
            with self.assertRaises(ValueError) as raised:
                server.validate_output_directory(str(profile))
            self.assertIn(str(reserved.resolve()), str(raised.exception))
            allowed = server.ANSIBLE_WORKING_DIR / "site"
            self.assertEqual(
                server.validate_output_directory(str(allowed))[0],
                allowed.resolve(),
            )
        finally:
            server.RESERVED_OUTPUT_ROOTS = original

    def test_invalid_inspection_is_rejected(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/inspections",
            {"uuid": "not-a-uuid", "inventory": {}},
        )
        self.assertEqual(status, 400)
        self.assertIn("valid node UUID", json.loads(body)["error"])

    def test_operator_can_create_named_host_before_any_ipa_report(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01", "ansibleGroup": "ignored_group"},
        )
        self.assertEqual(status, 201)
        placeholder = json.loads(body)
        self.assertTrue(placeholder["is_placeholder"])
        self.assertEqual(placeholder["preferences"]["ansibleHost"], "control-01")
        self.assertNotIn("ansibleGroup", placeholder["preferences"])

        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        nodes = json.loads(body)
        self.assertEqual(len(nodes), 1)
        self.assertTrue(nodes[0]["is_placeholder"])

    def test_inventory_names_cannot_be_created_more_than_once(self):
        status, _, _ = self.request("POST", "/api/v1/nodes", {"ansibleHost": "control-01"})
        self.assertEqual(status, 201)

        status, body, _ = self.request("POST", "/api/v1/nodes", {"ansibleHost": "control-01"})
        self.assertEqual(status, 400)
        self.assertIn("Duplicate Ansible host name", json.loads(body)["error"])

    def test_ready_commits_serial_linked_inventory_and_vars_to_workspace(self):
        self.request("POST", "/api/v1/inspections", self.report())
        sentinel = server.ANSIBLE_WORKING_DIR / "playbook.yml"
        sentinel.write_text("existing playbook must remain unchanged\n")
        destination = server.ANSIBLE_WORKING_DIR / "site"
        status, body, content_type = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(destination),
                "preferences": {
                    NODE_UUID: {
                        "rootDisk": "/dev/sda",
                        "netType": "single",
                        "ansibleHost": "server-01",
                    }
                },
                "blueprint": {
                    "clusterName": "cluster: #1",
                    "caCertBundle": "public CA",
                    "machineNetworkCidr": "10.20.0.0/16",
                    "dnsServers": ["10.20.0.53"],
                    "ntpServers": ["ntp.example.com"],
                    "pullSecret": "must-not-export",
                    "vaultPassword": "must-not-export",
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        response = json.loads(body)
        output_root = destination / "ocp-metal-ui"
        self.assertEqual(response["outputDirectory"], str(output_root))
        self.assertEqual(
            set(response["files"]),
            {
                "inventory.yml",
                "group_vars/ocp.yml",
                "host_vars/dc01.yml",
                "host_vars/dc02.yml",
                "host_vars/server-01.yml",
            },
        )
        inventory = (output_root / "inventory.yml").read_text()
        group_vars = (output_root / "group_vars/ocp.yml").read_text()
        host_vars = (output_root / "host_vars/server-01.yml").read_text()
        self.assertIn('"ocp"', inventory)
        self.assertIn('"server-01"', inventory)
        self.assertIn('"serial_number": "NODE-SERIAL-01"', inventory)
        self.assertNotIn("cluster_blueprint", group_vars)
        self.assertIn('"clusterName": "cluster: #1"', group_vars)
        self.assertIn('"machineNetworkCidr": "10.20.0.0/16"', group_vars)
        self.assertIn('"dnsServers":\n  - "10.20.0.53"', group_vars)
        self.assertIn('"ntpServers":\n  - "ntp.example.com"', group_vars)
        self.assertIn('"rootDisk": "/dev/sda"', host_vars)
        self.assertIn('"serial_number": "NODE-SERIAL-01"', host_vars)
        self.assertIn(NODE_UUID, host_vars)
        self.assertNotIn("must-not-export", group_vars)
        self.assertNotIn("vaultPassword", group_vars)
        self.assertEqual(sentinel.read_text(), "existing playbook must remain unchanged\n")
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["preferences"]["ansibleHost"], "server-01")
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        settings = json.loads(body)
        self.assertEqual(settings["blueprint"]["clusterName"], "cluster: #1")
        self.assertNotIn("pullSecret", settings["blueprint"])
        self.assertEqual(settings["redfishEndpoints"], [])
        self.assertEqual(settings["outputDirectory"], str(destination))

    def test_later_generation_replaces_host_vars_the_ui_no_longer_produces(self):
        self.request("POST", "/api/v1/inspections", self.report())
        destination = server.ANSIBLE_WORKING_DIR / "site"
        sentinel = server.ANSIBLE_WORKING_DIR / "playbook.yml"
        sentinel.write_text("existing playbook must remain unchanged\n")
        first = {
            "outputDirectory": str(destination),
            "preferences": {NODE_UUID: {"ansibleHost": "server-01", "netType": "single"}},
            "blueprint": {"baseDomain": "ocp.internal", "machineNetworkCidr": "10.20.0.0/16"},
        }
        status, _, _ = self.request("POST", "/api/v1/ansible-files", first)
        self.assertEqual(status, 200)
        output_root = destination / "ocp-metal-ui"
        (output_root / "host_vars" / "retired.yml").write_text("old host\n")
        (output_root / "host_vars" / "notes.txt").write_text("keep non-yaml\n")
        (output_root / "group_vars" / "vault.yml").write_text("operator group vars stay\n")
        (output_root / "keep.txt").write_text("workspace notes stay\n")
        second = {
            "outputDirectory": str(destination),
            "preferences": {NODE_UUID: {"ansibleHost": "server-02", "netType": "single"}},
            "blueprint": {"baseDomain": "ocp.internal", "machineNetworkCidr": "10.20.0.0/16"},
        }
        status, body, _ = self.request("POST", "/api/v1/ansible-files", second)
        self.assertEqual(status, 200)
        self.assertIn("host_vars/server-02.yml", json.loads(body)["files"])
        self.assertFalse((output_root / "host_vars" / "server-01.yml").exists())
        self.assertFalse((output_root / "host_vars" / "retired.yml").exists())
        self.assertEqual((output_root / "host_vars" / "notes.txt").read_text(), "keep non-yaml\n")
        self.assertEqual((output_root / "group_vars" / "vault.yml").read_text(), "operator group vars stay\n")
        self.assertEqual((output_root / "keep.txt").read_text(), "workspace notes stay\n")
        self.assertEqual(sentinel.read_text(), "existing playbook must remain unchanged\n")
        self.assertIn('"server-02"', (output_root / "inventory.yml").read_text())
        self.assertNotIn('"server-01"', (output_root / "inventory.yml").read_text())

    def test_export_rejects_nodes_without_serial_numbers(self):
        self.request(
            "POST",
            "/api/v1/inspections",
            {
                "uuid": NODE_UUID,
                "inventory": {"disks": [], "interfaces": []},
            },
        )
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "server-01"}
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("no serial number", json.loads(body)["error"])

    def test_named_placeholder_links_to_ipa_and_exports_pending_host(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01", "ansibleGroup": "ignored_group"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        status, _, _ = self.request("POST", "/api/v1/inspections", self.report())
        self.assertEqual(status, 202)

        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes/link",
            {"placeholderUuid": placeholder_uuid, "nodeUuid": NODE_UUID},
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        nodes = json.loads(body)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["uuid"], NODE_UUID)
        self.assertFalse(nodes[0]["is_placeholder"])
        self.assertEqual(nodes[0]["preferences"]["ansibleHost"], "control-01")
        self.assertEqual(nodes[0]["serial_number"], "NODE-SERIAL-01")

    def test_discovered_node_assignments_link_existing_or_new_inventory_hosts(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]

        self.request("POST", "/api/v1/inspections", self.report())
        second_uuid = "8a4f91c2-38d1-4bda-912c-bbfa47821002"
        second_report = self.report()
        second_report["uuid"] = second_uuid
        second_report["inventory"]["system_vendor"]["serial_number"] = "NODE-SERIAL-02"
        self.request("POST", "/api/v1/inspections", second_report)

        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {
                "assignments": [
                    {"nodeUuid": NODE_UUID, "placeholderUuid": placeholder_uuid},
                    {"nodeUuid": second_uuid, "ansibleHost": "worker-02"},
                ]
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["updated"], 2)

        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        nodes = {node["uuid"]: node for node in json.loads(body)}
        self.assertEqual(set(nodes), {NODE_UUID, second_uuid})
        self.assertEqual(nodes[NODE_UUID]["preferences"]["ansibleHost"], "control-01")
        self.assertEqual(nodes[second_uuid]["preferences"]["ansibleHost"], "worker-02")

    def test_discovered_node_assignments_reject_duplicate_hosts_atomically(self):
        self.request("POST", "/api/v1/inspections", self.report())
        second_uuid = "8a4f91c2-38d1-4bda-912c-bbfa47821002"
        second_report = self.report()
        second_report["uuid"] = second_uuid
        second_report["inventory"]["system_vendor"]["serial_number"] = "NODE-SERIAL-02"
        self.request("POST", "/api/v1/inspections", second_report)

        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {
                "assignments": [
                    {"nodeUuid": NODE_UUID, "ansibleHost": "worker-02"},
                    {"nodeUuid": second_uuid, "ansibleHost": "worker-02"},
                ]
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("Duplicate Ansible host name", json.loads(body)["error"])

        status, body, _ = self.request("GET", "/api/v1/nodes")
        nodes = json.loads(body)
        self.assertTrue(all(not node["preferences"].get("ansibleHost") for node in nodes))

    def test_preference_saves_reject_duplicate_inventory_names(self):
        self.request("POST", "/api/v1/inspections", self.report())
        second_uuid = "8a4f91c2-38d1-4bda-912c-bbfa47821002"
        second_report = self.report()
        second_report["uuid"] = second_uuid
        second_report["inventory"]["system_vendor"]["serial_number"] = "NODE-SERIAL-02"
        self.request("POST", "/api/v1/inspections", second_report)

        duplicate_preferences = {
            NODE_UUID: {"ansibleHost": "worker-01"},
            second_uuid: {"ansibleHost": "worker-01"},
        }
        status, body, _ = self.request(
            "PUT",
            "/api/v1/nodes/preferences",
            {"preferences": duplicate_preferences},
        )
        self.assertEqual(status, 400)
        self.assertIn("Duplicate Ansible host name", json.loads(body)["error"])

        status, body, _ = self.request(
            "PUT",
            "/api/v1/configuration",
            {
                "blueprint": {},
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": duplicate_preferences,
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("Duplicate Ansible host name", json.loads(body)["error"])

    def test_discovered_node_can_be_unlinked_relinked_and_deleted_without_losing_inventory_name(self):
        status, body, _ = self.request("POST", "/api/v1/nodes", {"ansibleHost": "control-01"})
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        self.request("POST", "/api/v1/inspections", self.report())

        status, _, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {"assignments": [{"nodeUuid": NODE_UUID, "placeholderUuid": placeholder_uuid}]},
        )
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {"assignments": [{"nodeUuid": NODE_UUID, "unlink": True}]},
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        nodes = {node["uuid"]: node for node in json.loads(body)}
        restored_placeholder_uuid = next(
            node_uuid for node_uuid, node in nodes.items() if node["is_placeholder"]
        )
        self.assertEqual(nodes[restored_placeholder_uuid]["preferences"]["ansibleHost"], "control-01")
        self.assertNotIn("ansibleHost", nodes[NODE_UUID]["preferences"])

        status, _, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {
                "assignments": [
                    {"nodeUuid": NODE_UUID, "placeholderUuid": restored_placeholder_uuid}
                ]
            },
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request("DELETE", f"/api/v1/nodes/{NODE_UUID}")
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        remaining_nodes = json.loads(body)
        self.assertEqual(len(remaining_nodes), 1)
        self.assertTrue(remaining_nodes[0]["is_placeholder"])
        self.assertEqual(remaining_nodes[0]["preferences"]["ansibleHost"], "control-01")

    def test_delete_removes_an_unlinked_discovered_node_and_reports_unknown_nodes(self):
        self.request("POST", "/api/v1/inspections", self.report())
        status, body, _ = self.request("DELETE", f"/api/v1/nodes/{NODE_UUID}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "deleted")
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(json.loads(body), [])

        status, body, _ = self.request("DELETE", f"/api/v1/nodes/{NODE_UUID}")
        self.assertEqual(status, 400)
        self.assertIn("no longer exists", json.loads(body)["error"])

    def test_placeholder_can_be_committed_before_ipa_reports_arrive(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    placeholder_uuid: {
                        "ansibleHost": "control-01",
                    }
                },
                "blueprint": {"clusterName": "new-cluster"},
            },
        )
        self.assertEqual(status, 200)
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        inventory = (output_root / "inventory.yml").read_text()
        host_vars = (output_root / "host_vars/control-01.yml").read_text()
        group_vars = (output_root / "group_vars/ocp.yml").read_text()
        self.assertIn('"control-01": {}', inventory)
        self.assertIn('"node_status": "awaiting_ipa"', host_vars)
        self.assertIn('"singleNic": "eno5"', host_vars)
        self.assertIn('"singleVlanEnabled": true', host_vars)
        self.assertIn('"singleVlanId": "281"', host_vars)
        self.assertIn('"bondVlanEnabled": true', host_vars)
        self.assertIn('"bondVlanId": "281"', host_vars)
        self.assertNotIn("cluster_blueprint", group_vars)
        self.assertIn('"clusterName": "new-cluster"', group_vars)
        self.assertIn('"machineNetworkCidr": "192.0.2.0/24"', group_vars)
        self.assertIn('"genesisHostname": "edge"', group_vars)
        self.assertIn('"genesisIp": "192.0.2.35"', group_vars)
        self.assertIn('"genesisNic": "eno5"', group_vars)
        self.assertIn('"networkGateway": "192.0.2.1"', group_vars)
        self.assertIn('"mgmtNetworkCidr": "198.51.100.0/24"', group_vars)
        self.assertIn('"mgmtVlan": "280"', group_vars)
        self.assertNotIn("mgmtDc01", group_vars)
        self.assertNotIn("mgmtDc02", group_vars)
        self.assertNotIn("ipv4LastOctet", host_vars)
        self.assertNotIn("isc_dhcpd_start_ip", group_vars)
        self.assertNotIn("bastionHostname", group_vars)
        self.assertNotIn("bastionIp", group_vars)
        self.assertIn('"dnsServers":\n  - "192.0.2.35"', group_vars)

    def test_retired_bastion_blueprint_keys_move_to_genesis_keys(self):
        with server.connect_database() as connection:
            connection.execute(
                "UPDATE app_settings SET value_json = ? WHERE key = 'blueprint'",
                (
                    json.dumps(
                        {
                            "clusterName": "kept-cluster",
                            "bastionHostname": "edge-genesis",
                            "bastionIp": "192.0.2.36",
                        }
                    ),
                ),
            )
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        blueprint = json.loads(body)["blueprint"]
        self.assertEqual(blueprint["clusterName"], "kept-cluster")
        self.assertEqual(blueprint["genesisHostname"], "edge-genesis")
        self.assertEqual(blueprint["genesisIp"], "192.0.2.36")
        self.assertNotIn("bastionHostname", blueprint)
        self.assertNotIn("bastionIp", blueprint)

        status, body, _ = self.request(
            "PUT",
            "/api/v1/configuration",
            {
                "blueprint": {"bastionHostname": "rejected"},
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("bastionHostname", json.loads(body)["error"])

        status, _, _ = self.request(
            "PUT",
            "/api/v1/configuration",
            {
                "blueprint": {"clusterName": "kept-cluster"},
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {},
            },
        )
        self.assertEqual(status, 200)
        with server.connect_database() as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT value_json FROM app_settings WHERE key = 'blueprint'"
                ).fetchone()["value_json"]
            )
        self.assertEqual(stored["genesisHostname"], "edge-genesis")
        self.assertEqual(stored["genesisIp"], "192.0.2.36")
        self.assertNotIn("bastionHostname", stored)
        self.assertNotIn("bastionIp", stored)

        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        status, _, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {placeholder_uuid: {"ansibleHost": "control-01"}},
                "blueprint": {
                    "bastionHostname": "edge-genesis",
                    "bastionIp": "192.0.2.36",
                },
            },
        )
        self.assertEqual(status, 200)
        group_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "group_vars" / "ocp.yml"
        ).read_text()
        self.assertIn('"genesisHostname": "edge-genesis"', group_vars)
        self.assertIn('"genesisIp": "192.0.2.36"', group_vars)
        self.assertNotIn("bastionHostname", group_vars)
        self.assertNotIn("bastionIp", group_vars)

    def test_genesis_nic_is_the_default_for_a_new_node_only(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        self.request("POST", "/api/v1/inspections", self.report())
        status, _, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    placeholder_uuid: {"ansibleHost": "control-01"},
                    NODE_UUID: {"ansibleHost": "server-01", "singleNic": "eno1"},
                },
                "blueprint": {"genesisNic": "eno7"},
            },
        )
        self.assertEqual(status, 200)
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        new_node = (output_root / "host_vars/control-01.yml").read_text()
        saved_nic = (output_root / "host_vars/server-01.yml").read_text()
        group_vars = (output_root / "group_vars/ocp.yml").read_text()
        self.assertIn('"singleNic": "eno7"', new_node)
        self.assertIn('"singleNic": "eno1"', saved_nic)
        self.assertIn('"genesisNic": "eno7"', group_vars)

    def test_management_last_octets_are_host_vars(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        status, _, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {placeholder_uuid: {"ansibleHost": "control-01"}},
                "blueprint": {
                    "mgmtNetworkCidr": "198.51.100.0/24",
                    "mgmtDc01Octet": "9",
                    "mgmtDc02Octet": "10",
                    "mgmtDc01Ip": "198.51.100.9",
                },
            },
        )
        self.assertEqual(status, 200)
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        inventory = (output_root / "inventory.yml").read_text()
        group_vars = (output_root / "group_vars/ocp.yml").read_text()
        dc01 = (output_root / "host_vars/dc01.yml").read_text()
        dc02 = (output_root / "host_vars/dc02.yml").read_text()
        ocp_section = inventory.split('"ocp":', 1)[1]
        mgmt_section = inventory.split('"ocp":', 1)[0]
        self.assertIn('"mgmt":', inventory)
        self.assertIn('"dc01": {}', mgmt_section)
        self.assertIn('"dc02": {}', mgmt_section)
        self.assertNotIn('"dc01"', ocp_section)
        self.assertNotIn('"dc02"', ocp_section)
        self.assertFalse((output_root / "group_vars/mgmt.yml").exists())
        self.assertIn('"mgmtNetworkCidr": "198.51.100.0/24"', group_vars)
        self.assertNotIn("mgmtDc01", group_vars)
        self.assertNotIn("mgmtDc02", group_vars)
        self.assertNotIn("198.51.100.9", group_vars)
        self.assertEqual(dc01, '"ipv4LastOctet": "9"\n')
        self.assertEqual(dc02, '"ipv4LastOctet": "10"\n')

    def test_stored_dc_addresses_become_last_octets(self):
        with server.connect_database() as connection:
            connection.execute(
                "UPDATE app_settings SET value_json = ? WHERE key = 'blueprint'",
                (
                    json.dumps(
                        {
                            "clusterName": "kept-cluster",
                            "mgmtDc01Ip": "198.51.100.16",
                            "mgmtDc02Ip": "198.51.100.17",
                        }
                    ),
                ),
            )
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        blueprint = json.loads(body)["blueprint"]
        self.assertEqual(blueprint["mgmtDc01Octet"], "16")
        self.assertEqual(blueprint["mgmtDc02Octet"], "17")
        self.assertNotIn("mgmtDc01Ip", blueprint)
        self.assertNotIn("mgmtDc02Ip", blueprint)

        status, _, _ = self.request(
            "PUT",
            "/api/v1/configuration",
            {
                "blueprint": {"mgmtDc01Ip": "198.51.100.4", "mgmtDc02Octet": "5"},
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {},
            },
        )
        self.assertEqual(status, 200)
        with server.connect_database() as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT value_json FROM app_settings WHERE key = 'blueprint'"
                ).fetchone()["value_json"]
            )
        self.assertEqual(stored["mgmtDc01Octet"], "4")
        self.assertEqual(stored["mgmtDc02Octet"], "5")
        self.assertNotIn("mgmtDc01Ip", stored)
        self.assertNotIn("mgmtDc02Ip", stored)

    def test_node_last_octet_is_a_host_var_and_blank_is_omitted(self):
        self.request("POST", "/api/v1/inspections", self.report())
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "server-01", "ipv4LastOctet": "40"},
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 200)
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "server-01.yml"
        ).read_text()
        self.assertEqual(host_vars.count("ipv4LastOctet"), 1)
        self.assertIn('\n"ipv4LastOctet": "40"\n', "\n" + host_vars)
        self.assertNotIn('  "ipv4LastOctet"', host_vars)

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "server-01", "ipv4LastOctet": ""},
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 200)
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "server-01.yml"
        ).read_text()
        self.assertNotIn("ipv4LastOctet", host_vars)

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "dc01", "ipv4LastOctet": "4"},
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("reserved", json.loads(body)["error"])

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {"ansibleHost": "server-01", "ipv4LastOctet": "nope"},
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("last octet", json.loads(body)["error"])

    def test_genesis_and_nodes_keep_every_nic_and_storage_device(self):
        report = self.report()
        report["inventory"]["disks"] = [
            {"name": "/dev/nvme0n1", "serial": "NVME-1", "model": "NVMe", "size": 1000},
            {"name": "/dev/sda", "serial": "HDD-1", "model": "HDD", "size": 2000},
        ]
        report["inventory"]["interfaces"] = [
            {"name": "eno1", "mac_address": "52:54:00:fa:19:bc", "state": "up"},
            {"name": "eno6", "mac_address": "52:54:00:aa:bb:cc", "state": "down"},
        ]
        self.request("POST", "/api/v1/inspections", report)
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {NODE_UUID: {"ansibleHost": "server-01"}},
                "blueprint": {
                    "genesisNic": "eno5",
                    "genesisNics": ["eno6", "eno5", "eno6"],
                    "genesisDisks": ["/dev/nvme0n1", "/dev/sda", "/dev/sda"],
                },
            },
        )
        self.assertEqual(status, 200, body)
        group_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "group_vars" / "ocp.yml"
        ).read_text()
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "server-01.yml"
        ).read_text()
        self.assertIn('"genesisNic": "eno5"', group_vars)
        self.assertIn('- "eno6"\n  - "eno5"', group_vars)
        self.assertEqual(group_vars.count('"eno6"'), 1)
        self.assertIn('- "/dev/nvme0n1"\n  - "/dev/sda"', group_vars)
        self.assertEqual(host_vars.count("/dev/nvme0n1"), 1)
        self.assertEqual(host_vars.count("/dev/sda"), 1)
        self.assertIn('"eno1"', host_vars)
        self.assertIn('"eno6"', host_vars)

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {NODE_UUID: {"ansibleHost": "server-01"}},
                "blueprint": {"genesisNic": "eno7", "genesisNics": ["eno6"]},
            },
        )
        self.assertEqual(status, 200, body)
        group_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "group_vars" / "ocp.yml"
        ).read_text()
        self.assertIn('- "eno7"\n  - "eno6"', group_vars)

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {NODE_UUID: {"ansibleHost": "server-01"}},
                "blueprint": {"genesisDisks": ["Samsung SSD"]},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("device names", json.loads(body)["error"])

    def test_explicit_vlan_disable_is_kept(self):
        self.request("POST", "/api/v1/inspections", self.report())
        status, _, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {
                        "ansibleHost": "server-01",
                        "netType": "single",
                        "singleVlanEnabled": False,
                        "singleVlanId": "",
                    }
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 200)
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "server-01.yml"
        ).read_text()
        self.assertIn('"singleVlanEnabled": false', host_vars)
        self.assertIn('"singleVlanId": ""', host_vars)

    def test_ready_refuses_paths_outside_configured_workspace(self):
        self.request("POST", "/api/v1/inspections", self.report())
        outside_path = str(Path(self.temp_dir.name).parent / "outside-ansible")
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": outside_path,
                "preferences": {
                    NODE_UUID: {"ansibleHost": "server-01"}
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("inside the configured Ansible workspace", json.loads(body)["error"])

    def test_configuration_api_exposes_default_workspace(self):
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertEqual(config["ansibleWorkingDir"], str(server.ANSIBLE_WORKING_DIR.resolve()))
        self.assertEqual(config["defaultOutputDirectory"], config["ansibleWorkingDir"])
        self.assertEqual(config["outputDirectory"], config["ansibleWorkingDir"])
        self.assertEqual(config["blueprint"], server.DEFAULT_BLUEPRINT)
        self.assertEqual(config["redfishEndpoints"], [])
        self.assertFalse(config["httpsEnabled"])

    def test_redfish_profiles_persist_without_credentials(self):
        endpoints = [
            {
                "id": "bmc-1",
                "name": "Control node 1",
                "baseUrl": "https://192.0.2.20",
                "ansibleHost": "control-01",
                "ansibleGroup": "control_plane",
            }
        ]
        expected = [
            {
                "id": "bmc-1",
                "name": "Control node 1",
                "baseUrl": "https://192.0.2.20",
            }
        ]
        status, body, _ = self.request(
            "PUT", "/api/v1/redfish/endpoints", {"endpoints": endpoints}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["endpoints"], expected)
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["redfishEndpoints"], expected)
        self.assertNotIn("password", body.decode().lower())

        database_contents = self.database_bytes()
        self.assertNotIn(b"password", database_contents.lower())

    def test_redfish_profiles_require_https_urls_and_unique_ids(self):
        invalid_profiles = [
            [
                {
                    "id": "bmc-1",
                    "name": "BMC 1",
                    "baseUrl": "http://192.0.2.20",
                    "ansibleHost": "host-01",
                }
            ],
            [
                {
                    "id": "same",
                    "name": "BMC 1",
                    "baseUrl": "https://192.0.2.20",
                    "ansibleHost": "host-01",
                },
                {
                    "id": "same",
                    "name": "BMC 2",
                    "baseUrl": "https://192.0.2.21",
                    "ansibleHost": "host-02",
                },
            ],
        ]
        for endpoints in invalid_profiles:
            with self.subTest(endpoints=endpoints):
                status, body, _ = self.request(
                    "PUT", "/api/v1/redfish/endpoints", {"endpoints": endpoints}
                )
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_redfish_retrieval_rejects_http_before_processing_credentials(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/redfish/retrieve",
            {
                "endpointId": "bmc-1",
                "username": "bmc-user",
                "password": "transient-secret",
                "allowUntrustedTls": False,
            },
        )
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", json.loads(body)["error"])
        self.assertNotIn("transient-secret", body.decode())

    def test_redfish_retrieval_persists_inventory_without_saving_credentials(self):
        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes",
            {"ansibleHost": "control-01"},
        )
        self.assertEqual(status, 201)
        placeholder_uuid = json.loads(body)["uuid"]
        endpoint = {
            "id": "bmc-1",
            "name": "Control node 1",
            "baseUrl": "https://192.0.2.20",
            "ansibleGroup": "control_plane",
        }
        status, _, _ = self.request(
            "PUT", "/api/v1/redfish/endpoints", {"endpoints": [endpoint]}
        )
        self.assertEqual(status, 200)
        report = {
            "uuid": NODE_UUID,
            "serial_number": "RF-SERIAL-01",
            "inventory": {
                "system": {"uuid": NODE_UUID},
                "cpus": {"count": 2, "model": "Example CPU"},
                "memory": {"total": 4096},
                "disks": [],
                "interfaces": [],
            },
            "warnings": [],
        }
        response_values = []

        class FakeTLSConnection:
            pass

        handler = object.__new__(server.ApiHandler)
        handler.connection = FakeTLSConnection()
        handler.send_json = lambda status, value: response_values.append((status, value))
        with (
            patch.object(server.ssl, "SSLSocket", FakeTLSConnection),
            patch.object(server, "RedfishClient") as redfish_client,
        ):
            redfish_client.return_value.retrieve.return_value = report
            handler.retrieve_redfish_endpoint(
                {
                    "endpointId": "bmc-1",
                    "username": "operator",
                    "password": "transient-secret",
                    "allowUntrustedTls": False,
                }
            )

        redfish_client.return_value.retrieve.assert_called_once_with(
            "operator", "transient-secret"
        )
        self.assertEqual(response_values[0][0], 200)
        self.assertFalse(response_values[0][1]["credentialsStored"])
        self.assertNotIn("transient-secret", json.dumps(response_values[0][1]))
        self.assertNotIn(b"transient-secret", self.database_bytes())

        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        nodes = json.loads(body)
        node = next(node for node in nodes if node["uuid"] == NODE_UUID)
        self.assertEqual(node["source"], "redfish")
        self.assertEqual(node["serial_number"], "RF-SERIAL-01")
        self.assertNotIn("ansibleHost", node["preferences"])

        status, body, _ = self.request(
            "POST",
            "/api/v1/nodes/assignments",
            {
                "assignments": [
                    {"nodeUuid": NODE_UUID, "placeholderUuid": placeholder_uuid}
                ]
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["updated"], 1)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        assigned_node = next(node for node in json.loads(body) if node["uuid"] == NODE_UUID)
        self.assertEqual(assigned_node["preferences"]["ansibleHost"], "control-01")

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {
                        "ansibleHost": "control-01",
                    }
                },
                "blueprint": {},
            },
        )
        self.assertEqual(status, 200)
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        inventory = (output_root / "inventory.yml").read_text()
        host_vars = (output_root / "host_vars/control-01.yml").read_text()
        self.assertIn('"bmc_address": "https://192.0.2.20"', inventory)
        self.assertNotIn('"ansible_host":', inventory)
        self.assertIn('"redfish_inventory"', host_vars)
        self.assertIn('"inventory_source": "redfish"', host_vars)
        self.assertIn('"ocp"', inventory)
        self.assertTrue((output_root / "group_vars/ocp.yml").is_file())


    def _post_inspection(self, node_uuid, serial, nics, disks, current_ip=""):
        status, body, _ = self.request(
            "POST",
            "/api/v1/inspections",
            {
                "uuid": node_uuid,
                "bmc_address": "",
                "inventory": {
                    "cpu": {"count": 32, "model_name": "Example CPU"},
                    "memory": {"physical_mb": 65536},
                    "system_vendor": {"serial_number": serial},
                    "current_ip": current_ip,
                    "disks": [
                        {"name": name, "serial": disk_serial, "size": 1000}
                        for name, disk_serial in disks
                    ],
                    "interfaces": [
                        {"name": name, "mac_address": mac, "state": "up"}
                        for name, mac in nics
                    ],
                },
            },
        )
        self.assertEqual(status, 202, body)
        return node_uuid

    def test_publish_round_trips_into_install_yaml_and_the_database_keeps_the_facts(self):
        import sys

        import yaml

        from agent_install import agent_hosts

        render_templates = None
        playbooks = playbook_root()
        if playbooks is not None and (playbooks / "tests").is_dir():
            sys.path.insert(0, str(playbooks / "tests"))
            from test_agent_install import render_templates

        specs = [
            {
                "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821001",
                "ansibleHost": "control-01",
                "role": "master",
                "rendezvous": True,
                "ipv4LastOctet": "011",
                "current_ip": "192.0.2.99",
                "serial": "NODE-SERIAL-01",
                "rootDisk": "NVME-C1",
                "nics": [("eno5", "52:54:00:AA:00:05"), ("eno6", "52:54:00:11:00:06")],
                "disks": [("/dev/nvme0n1", "NVME-C1")],
                "preferences": {
                    "netType": "single",
                    "singleNic": "eno5",
                    "singleVlanEnabled": True,
                    "singleVlanId": "281",
                },
            },
            {
                "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821002",
                "ansibleHost": "control-02",
                "role": "master",
                "rendezvous": False,
                "ipv4LastOctet": "12",
                "current_ip": "",
                "serial": "NODE-SERIAL-02",
                "rootDisk": "NVME-C2",
                "nics": [("eno5", "52:54:00:12:00:05"), ("eno6", "52:54:00:12:00:06")],
                "disks": [("/dev/nvme0n1", "NVME-C2")],
                "preferences": {
                    "netType": "bond",
                    "bondNics": ["eno5", "eno6"],
                    "bondVlanEnabled": False,
                    "bondVlanId": "",
                },
            },
            {
                "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821003",
                "ansibleHost": "control-03",
                "role": "master",
                "rendezvous": False,
                "ipv4LastOctet": "13",
                "current_ip": "",
                "serial": "NODE-SERIAL-03",
                "rootDisk": "NVME-C3",
                "nics": [("eno5", "52:54:00:13:00:05")],
                "disks": [("/dev/nvme0n1", "NVME-C3")],
                "preferences": {
                    "netType": "single",
                    "singleNic": "eno5",
                    "singleVlanEnabled": False,
                    "singleVlanId": "",
                },
            },
            {
                "uuid": "8a4f91c2-38d1-4bda-912c-bbfa47821004",
                "ansibleHost": "worker-01",
                "role": "worker",
                "rendezvous": False,
                "ipv4LastOctet": "14",
                "current_ip": "",
                "serial": "NODE-SERIAL-04",
                "rootDisk": "NVME-W1",
                "nics": [("eno5", "52:54:00:14:00:05"), ("eno6", "52:54:00:14:00:06")],
                "disks": [("/dev/nvme0n1", "NVME-W1")],
                "preferences": {
                    "netType": "bond",
                    "bondNics": ["eno5", "eno6"],
                    "bondVlanEnabled": True,
                    "bondVlanId": "281",
                },
            },
        ]
        preferences = {}
        for spec in specs:
            self._post_inspection(
                spec["uuid"],
                spec["serial"],
                spec["nics"],
                spec["disks"],
                spec["current_ip"],
            )
            preferences[spec["uuid"]] = {
                "ansibleHost": spec["ansibleHost"],
                "role": spec["role"],
                "rendezvous": spec["rendezvous"],
                "ipv4LastOctet": spec["ipv4LastOctet"],
                "rootDisk": spec["rootDisk"],
                **spec["preferences"],
            }
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": preferences,
                "blueprint": {
                    "clusterName": "ocp-baremetal",
                    "baseDomain": "ocp.internal",
                    "controlPlaneTopology": "HighlyAvailable",
                    "machineNetworkCidr": "192.0.2.0/24",
                    "networkGateway": "192.0.2.1",
                    "dnsServers": ["192.0.2.35"],
                    "ntpServers": ["192.0.2.35"],
                    "apiVip": "192.0.2.2",
                    "ingressVip": "192.0.2.3",
                    "caCertBundle": "TEST-CA",
                    "pullSecret": "must-not-export",
                    "vaultPassword": "must-not-export",
                },
            },
        )
        self.assertEqual(status, 200, body)
        published_preview = json.loads(body)
        self.assertEqual(published_preview["configPreviewError"], "")
        self.assertNotIn("install-config.yaml", published_preview["files"])
        self.assertNotIn("agent-config.yaml", published_preview["files"])
        self.assertIn("<supplied at install>", published_preview["installConfig"])
        self.assertNotIn("auths", published_preview["installConfig"])
        self.assertEqual(published_preview["installConfigFile"], "")
        self.assertEqual(published_preview["agentConfigFile"], "")
        self.assertIn("pull secret", published_preview["installFilesMessage"])
        self.assertFalse(
            (server.ANSIBLE_WORKING_DIR / "cluster-install" / "install-config.yaml").exists()
        )
        self.assertFalse(
            (server.ANSIBLE_WORKING_DIR / "cluster-install" / "agent-config.yaml").exists()
        )
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        published = "\n".join(
            path.read_text() for path in sorted(output_root.rglob("*")) if path.is_file()
        )
        self.assertNotIn("must-not-export", published)
        self.assertNotIn("pullSecret", published)
        self.assertNotIn("vaultPassword", published)

        group = yaml.safe_load((output_root / "group_vars" / "ocp.yml").read_text())
        self.assertEqual(group["apiVip"], "192.0.2.2")
        self.assertEqual(group["ingressVip"], "192.0.2.3")
        self.assertEqual(group["clusterName"], "ocp-baremetal")
        self.assertNotIn("mgmtDc01Octet", group)

        inventory = yaml.safe_load((output_root / "inventory.yml").read_text())
        ocp_hosts = inventory["all"]["children"]["ocp"]["hosts"]
        self.assertEqual(
            set(ocp_hosts),
            {"control-01", "control-02", "control-03", "worker-01"},
        )
        self.assertEqual(ocp_hosts["control-01"]["serial_number"], "NODE-SERIAL-01")
        self.assertNotIn("bmc_address", ocp_hosts["control-01"])
        self.assertNotIn("ansible_host", ocp_hosts["control-01"])
        self.assertEqual(
            set(inventory["all"]["children"]["mgmt"]["hosts"]),
            {"dc01", "dc02"},
        )

        loaded_hosts = []
        for name in ocp_hosts:
            variables = yaml.safe_load((output_root / "host_vars" / f"{name}.yml").read_text())
            loaded_hosts.append({"name": name, "variables": variables})
        by_file = {item["name"]: item["variables"] for item in loaded_hosts}
        self.assertEqual(by_file["control-01"]["role"], "master")
        self.assertIs(by_file["control-01"]["rendezvous"], True)
        self.assertEqual(by_file["control-01"]["ipv4LastOctet"], "11")
        self.assertEqual(by_file["control-01"]["ipv4_ip"], "192.0.2.11")
        self.assertEqual(by_file["control-01"]["ipa_inventory"]["interfaces"][0]["mac_address"], "52:54:00:AA:00:05")
        self.assertEqual(by_file["control-02"]["node_preferences"]["bondNics"], ["eno5", "eno6"])
        self.assertIs(by_file["control-02"]["node_preferences"]["bondVlanEnabled"], False)
        self.assertIs(by_file["control-03"]["node_preferences"]["singleVlanEnabled"], False)
        self.assertEqual(by_file["worker-01"]["role"], "worker")
        self.assertEqual(by_file["worker-01"]["ipv4_ip"], "192.0.2.14")
        self.assertEqual(
            yaml.safe_load((output_root / "host_vars" / "dc01.yml").read_text()),
            {"ipv4LastOctet": "6"},
        )

        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertEqual(status, 200, body)
        stored_blueprint = json.loads(body)["blueprint"]
        self.assertEqual(stored_blueprint["apiVip"], "192.0.2.2")
        self.assertEqual(stored_blueprint["ingressVip"], "192.0.2.3")
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200, body)
        stored = {node["uuid"]: node for node in json.loads(body)}
        self.assertEqual(stored[specs[0]["uuid"]]["preferences"]["role"], "master")
        self.assertIs(stored[specs[0]["uuid"]]["preferences"]["rendezvous"], True)
        self.assertEqual(stored[specs[0]["uuid"]]["preferences"]["ipv4LastOctet"], "011")
        self.assertEqual(stored[specs[0]["uuid"]]["preferences"]["rootDisk"], "NVME-C1")
        self.assertEqual(stored[specs[3]["uuid"]]["preferences"]["role"], "worker")
        self.assertIs(stored[specs[3]["uuid"]]["preferences"]["rendezvous"], False)

        result = agent_hosts({"blueprint": group, "hosts": loaded_hosts})
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["platform"], "baremetal")
        self.assertEqual(result["rendezvous_ip"], "192.0.2.11")
        self.assertEqual(result["api_vips"], ["192.0.2.2"])
        self.assertEqual(result["ingress_vips"], ["192.0.2.3"])
        # The Jinja comparison needs the playbook checkout named by
        # OCP_ANSIBLE_PLAYBOOKS. The assertions above use this repo only.
        if render_templates is None:
            return
        rendered = render_templates(result)
        install_config = yaml.safe_load(rendered["install-config.yaml"])
        agent_config = yaml.safe_load(rendered["agent-config.yaml"])
        self.assertEqual(install_config["platform"]["baremetal"]["apiVIPs"], ["192.0.2.2"])
        self.assertEqual(install_config["platform"]["baremetal"]["ingressVIPs"], ["192.0.2.3"])
        self.assertEqual(install_config["controlPlane"]["replicas"], 3)
        self.assertEqual(install_config["compute"][0]["replicas"], 1)
        self.assertIn("TEST-CA", rendered["install-config.yaml"])
        self.assertIn('{"auths":{}}', rendered["install-config.yaml"])
        self.assertEqual(agent_config["rendezvousIP"], "192.0.2.11")
        recovered = {item["hostname"]: item for item in agent_config["hosts"]}
        self.assertEqual(recovered["control-01"]["role"], "master")
        self.assertEqual(recovered["control-01"]["rootDeviceHints"]["serialNumber"], "NVME-C1")
        self.assertEqual(recovered["control-01"]["interfaces"][0]["macAddress"], "52:54:00:aa:00:05")
        self.assertEqual(recovered["control-01"]["networkConfig"]["interfaces"][1]["name"], "eno5.281")
        self.assertEqual(
            recovered["control-01"]["networkConfig"]["interfaces"][1]["ipv4"]["address"][0]["ip"],
            "192.0.2.11",
        )
        self.assertEqual(recovered["control-02"]["networkConfig"]["interfaces"][2]["name"], "bond0")
        self.assertEqual(
            recovered["control-02"]["networkConfig"]["interfaces"][2]["link-aggregation"]["port"],
            ["eno5", "eno6"],
        )
        self.assertNotIn("vlan", recovered["control-02"]["networkConfig"]["interfaces"][2])
        self.assertEqual(recovered["control-03"]["networkConfig"]["interfaces"][0]["name"], "eno5")
        self.assertEqual(recovered["control-03"]["interfaces"][0]["macAddress"], "52:54:00:13:00:05")
        self.assertEqual(recovered["worker-01"]["role"], "worker")
        self.assertEqual(recovered["worker-01"]["networkConfig"]["interfaces"][3]["name"], "bond0.281")
        self.assertEqual(
            recovered["worker-01"]["networkConfig"]["routes"]["config"][0]["next-hop-interface"],
            "bond0.281",
        )
        self.assertEqual(recovered["worker-01"]["rootDeviceHints"]["serialNumber"], "NVME-W1")

        def normalized_install(document):
            normalized = dict(document)
            normalized["pullSecret"] = str(normalized.get("pullSecret", "")).strip()
            normalized["sshKey"] = str(normalized.get("sshKey", "")).strip()
            if "additionalTrustBundle" in normalized:
                normalized["additionalTrustBundle"] = str(normalized["additionalTrustBundle"]).strip()
            return normalized

        jinja_install = yaml.safe_load(rendered["install-config.yaml"])
        jinja_install["pullSecret"] = "<supplied at install>"
        jinja_install["sshKey"] = "<supplied at install>"
        self.assertEqual(
            normalized_install(yaml.safe_load(published_preview["installConfig"])),
            normalized_install(jinja_install),
        )
        self.assertEqual(
            yaml.safe_load(published_preview["agentConfig"]),
            yaml.safe_load(rendered["agent-config.yaml"]),
        )
        self.assertFalse((output_root / "install-config.yaml").exists())
        self.assertFalse((output_root / "agent-config.yaml").exists())
        self.assertFalse(
            (server.ANSIBLE_WORKING_DIR / "cluster-install" / "install-config.yaml").exists()
        )

    def test_blank_vips_stay_in_the_database_and_are_left_out_of_group_vars(self):
        self.request("POST", "/api/v1/inspections", self.report(bmc_address=""))
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {NODE_UUID: {"ansibleHost": "server-01", "role": "master"}},
                "blueprint": {"apiVip": "", "ingressVip": "  "},
            },
        )
        self.assertEqual(status, 200, body)
        group_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "group_vars" / "ocp.yml"
        ).read_text()
        self.assertNotIn("apiVip", group_vars)
        self.assertNotIn("ingressVip", group_vars)
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "server-01.yml"
        ).read_text()
        self.assertIn('"role": "master"', host_vars)
        self.assertNotIn("rendezvous", host_vars)
        status, body, _ = self.request("GET", "/api/v1/config")
        blueprint = json.loads(body)["blueprint"]
        self.assertEqual(blueprint["apiVip"], "")
        self.assertEqual(blueprint["ingressVip"], "")

    def test_partial_inventory_publishes_and_the_install_filter_requires_three_masters(self):
        import yaml

        from agent_install import agent_hosts

        self._post_inspection(
            NODE_UUID,
            "NODE-SERIAL-01",
            [("eno5", "52:54:00:11:00:05")],
            [("/dev/nvme0n1", "NVME-C1")],
        )
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {
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
                    "controlPlaneTopology": "HighlyAvailable",
                    "apiVip": "192.0.2.2",
                    "ingressVip": "192.0.2.3",
                },
            },
        )
        self.assertEqual(status, 200, body)
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        group = yaml.safe_load((output_root / "group_vars" / "ocp.yml").read_text())
        variables = yaml.safe_load((output_root / "host_vars" / "control-01.yml").read_text())
        result = agent_hosts(
            {"blueprint": group, "hosts": [{"name": "control-01", "variables": variables}]}
        )
        self.assertFalse(result["ok"])
        self.assertIn("Highly Available needs three masters.", result["error"])

    def test_publish_rejects_invalid_rendezvous_and_vips_without_writing(self):
        self._post_inspection(
            NODE_UUID,
            "NODE-SERIAL-01",
            [("eno5", "52:54:00:11:00:05")],
            [("/dev/nvme0n1", "NVME-C1")],
        )
        worker_uuid = "2b7e6c14-90af-4d55-8c31-0e6a91d4b702"
        self._post_inspection(
            worker_uuid,
            "NODE-SERIAL-02",
            [("eno5", "52:54:00:12:00:05"), ("eno6", "52:54:00:12:00:06")],
            [("/dev/nvme0n1", "NVME-W1")],
        )
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        cases = [
            (
                "Only one node can be the rendezvous host.",
                {
                    NODE_UUID: {"ansibleHost": "control-01", "role": "master", "rendezvous": True},
                    worker_uuid: {"ansibleHost": "control-02", "role": "master", "rendezvous": True},
                },
                {},
            ),
            (
                "rendezvous host must be a master.",
                {
                    NODE_UUID: {"ansibleHost": "control-01", "role": "worker", "rendezvous": True},
                    worker_uuid: {"ansibleHost": "worker-01", "role": "worker", "rendezvous": False},
                },
                {},
            ),
            (
                "must be an IPv4 address.",
                {
                    NODE_UUID: {"ansibleHost": "control-01", "role": "master"},
                    worker_uuid: {"ansibleHost": "worker-01", "role": "worker"},
                },
                {"apiVip": "not-an-ip"},
            ),
        ]
        for expected, node_preferences, blueprint in cases:
            with self.subTest(expected=expected):
                status, body, _ = self.request(
                    "POST",
                    "/api/v1/ansible-files",
                    {
                        "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                        "preferences": node_preferences,
                        "blueprint": blueprint,
                    },
                )
                self.assertEqual(status, 400, body)
                self.assertIn(expected, json.loads(body)["error"])
                self.assertFalse(output_root.exists())


    def test_a_node_can_publish_master_worker_and_storage_together(self):
        self._post_inspection(
            NODE_UUID,
            "NODE-SERIAL-01",
            [("eno5", "52:54:00:11:00:05")],
            [("/dev/nvme0n1", "NVME-C1")],
        )
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {
                        "ansibleHost": "control-01",
                        "roles": ["storage", "worker", "master"],
                        "rendezvous": True,
                        "ipv4LastOctet": "11",
                        "rootDisk": "NVME-C1",
                    }
                },
                "blueprint": {"controlPlaneTopology": "Compact"},
            },
        )
        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertIn("Compact needs three nodes.", preview["configPreviewError"])
        self.assertEqual(preview["installConfig"], "")
        self.assertEqual(preview["agentConfig"], "")
        self.assertEqual(preview["installConfigFile"], "")
        self.assertEqual(preview["agentConfigFile"], "")
        self.assertEqual(preview["installFilesMessage"], "")
        host_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "host_vars" / "control-01.yml"
        ).read_text()
        self.assertIn('"roles":\n  - "master"\n  - "worker"\n  - "storage"', host_vars)
        self.assertIn('"role": "master"', host_vars)
        group_vars = (
            server.ANSIBLE_WORKING_DIR / "ocp-metal-ui" / "group_vars" / "ocp.yml"
        ).read_text()
        self.assertIn('"controlPlaneTopology": "Compact"', group_vars)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        preferences = json.loads(body)[0]["preferences"]
        self.assertEqual(preferences["roles"], ["master", "worker", "storage"])
        self.assertEqual(preferences["role"], "master")
        self.assertIs(preferences["rendezvous"], True)

        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": str(server.ANSIBLE_WORKING_DIR),
                "preferences": {
                    NODE_UUID: {
                        "ansibleHost": "control-01",
                        "roles": ["storage"],
                        "rendezvous": True,
                    }
                },
                "blueprint": {"controlPlaneTopology": "Compact"},
            },
        )
        self.assertEqual(status, 400, body)
        self.assertIn("rendezvous host must be a master", json.loads(body)["error"])

    def test_bundled_install_filter_matches_the_playbook_filter(self):
        playbooks = playbook_root()
        if playbooks is None:
            self.skipTest(
                "Set OCP_ANSIBLE_PLAYBOOKS to an ocp-ansible-playbooks/playbooks "
                "checkout to compare the bundled filter."
            )
        bundled = Path(server.__file__).resolve().parent / "agent_install.py"
        playbook = playbooks / "filter_plugins" / "agent_install.py"
        self.assertEqual(bundled.read_bytes(), playbook.read_bytes())

    def test_render_play_can_read_saved_install_secret_files(self):
        playbooks = playbook_root()
        if playbooks is None:
            self.skipTest(
                "Set OCP_ANSIBLE_PLAYBOOKS to an ocp-ansible-playbooks/playbooks "
                "checkout to read render_agent_install.yml."
            )
        playbook = (playbooks / "render_agent_install.yml").read_text()
        self.assertIn("ocp_install_pull_secret_file", playbook)
        self.assertIn("ocp_install_ssh_public_key_file", playbook)
        self.assertIn("ocp_pre_install_pull_secret_contents | default('') | length == 0", playbook)
        self.assertIn("ocp_pre_install_ssh_key_contents | default('') | length == 0", playbook)
        self.assertIn("ocp_pre_install_pull_secret_contents | length > 0", playbook)

    def test_install_secret_routes_require_https_and_reject_bad_input(self):
        destination = str(server.ANSIBLE_WORKING_DIR)
        secret = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
        status, body, _ = self.request(
            "POST",
            "/api/v1/install-secrets/pull-secret",
            {"outputDirectory": destination, "pullSecret": secret},
        )
        self.assertEqual(status, 426)
        self.assertNotIn(b"dGVzdA==", body)
        self.assertFalse((server.ANSIBLE_WORKING_DIR / "cluster-install").exists())
        status, body, _ = self.request(
            "POST",
            "/api/v1/install-secrets/ssh-key",
            {"outputDirectory": destination, "replace": False},
        )
        self.assertEqual(status, 426, body)
        status, body, _ = self.request(
            "POST",
            "/api/v1/install-secrets/ssh-public-key",
            {"outputDirectory": destination, "sshPublicKey": "ssh-ed25519 AAAA comment"},
        )
        self.assertEqual(status, 426, body)
        status, body, _ = self.request(
            "GET",
            "/api/v1/install-secrets/ssh-private-key?outputDirectory=" + quote(destination),
        )
        self.assertEqual(status, 426)
        self.assertNotIn(b"PRIVATE KEY", body)
        with self.assertRaises(ValueError) as caught:
            server.normalize_ssh_public_key(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nAAA\n-----END OPENSSH PRIVATE KEY-----\n"
            )
        self.assertIn("private key", str(caught.exception).lower())
        with self.assertRaises(ValueError):
            server.save_pull_secret(destination, "{}")
        with self.assertRaises(ValueError):
            server.save_pull_secret(destination, "not-json")
        self.assertFalse((server.ANSIBLE_WORKING_DIR / "cluster-install").exists())
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        (server.ANSIBLE_WORKING_DIR / "cluster-install").symlink_to(outside, target_is_directory=True)
        self._vault_password()
        with self.assertRaises(ValueError):
            server.save_pull_secret(destination, secret)
        self.assertFalse((outside / "pull-secret.json").exists())
        self.assertFalse((outside / "pull-secret.vault").exists())

    def test_saved_pull_secret_and_ssh_key_fill_the_preview_only(self):
        import yaml

        self.assertIsNotNone(shutil.which("ssh-keygen"))
        destination = str(server.ANSIBLE_WORKING_DIR)
        secret = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
        self._vault_password()
        saved = server.save_pull_secret(destination, secret)
        self.assertTrue(saved["pullSecretStored"])
        self.assertNotIn("pullSecret", saved)
        self.assertNotIn("dGVzdA==", json.dumps(saved))
        created = server.generate_cluster_ssh_key(destination, replace=False)
        self.assertTrue(created["privateKeyStored"])
        self.assertTrue(created["sshPublicKey"].startswith("ssh-ed25519 "))
        self.assertIn(" ocp", created["sshPublicKey"])
        self.assertNotIn("PRIVATE KEY", json.dumps(created))
        with self.assertRaises(server.InstallSecretError) as caught:
            server.generate_cluster_ssh_key(destination, replace=False)
        self.assertEqual(caught.exception.status, 409)
        secret_dir = server.ANSIBLE_WORKING_DIR / "cluster-install"
        private_path = secret_dir / "id_ed25519"
        before = hashlib.sha256(private_path.read_bytes()).hexdigest()
        replaced = server.generate_cluster_ssh_key(destination, replace=True)
        after = hashlib.sha256(private_path.read_bytes()).hexdigest()
        self.assertNotEqual(before, after)
        self.assertNotEqual(created["sshPublicKey"], replaced["sshPublicKey"])
        self.assertEqual(stat.S_IMODE(secret_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((secret_dir / "pull-secret.vault").stat().st_mode), 0o600)
        self.assertIn("$ANSIBLE_VAULT;1.1;AES256", (secret_dir / "pull-secret.vault").read_text())
        self.assertNotIn("dGVzdA==", (secret_dir / "pull-secret.vault").read_text())
        self.assertFalse((secret_dir / "pull-secret.json").exists())
        self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((secret_dir / "id_ed25519.pub").stat().st_mode), 0o644)
        self.assertEqual(
            private_path.read_bytes().splitlines()[0],
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
        )
        same = server.save_ssh_public_key(destination, replaced["sshPublicKey"], replace=False)
        self.assertEqual(hashlib.sha256(private_path.read_bytes()).hexdigest(), after)
        self.assertTrue(same["privateKeyStored"])
        pasted = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAIQ== pasted-key"
        with self.assertRaises(server.InstallSecretError) as caught:
            server.save_ssh_public_key(destination, pasted, replace=False)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(hashlib.sha256(private_path.read_bytes()).hexdigest(), after)
        pasted_status = server.save_ssh_public_key(destination, pasted, replace=True)
        self.assertFalse(pasted_status["privateKeyStored"])
        self.assertEqual(pasted_status["sshPublicKey"], pasted)
        self.assertFalse(private_path.exists())
        server.generate_cluster_ssh_key(destination, replace=False)
        with patch("server.shutil.which", return_value=None):
            with self.assertRaises(server.InstallSecretError) as caught:
                server.generate_cluster_ssh_key(str(server.ANSIBLE_WORKING_DIR / "nossh"), replace=False)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("ssh-keygen", str(caught.exception))
        self.assertFalse((server.ANSIBLE_WORKING_DIR / "nossh" / "cluster-install").exists())

        self._post_inspection(
            NODE_UUID,
            "NODE-SERIAL-01",
            [("eno5", "52:54:00:11:00:05")],
            [("/dev/nvme0n1", "NVME-C1")],
        )
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": destination,
                "preferences": {
                    NODE_UUID: {
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
                    "pullSecret": "must-not-export",
                    "vaultPassword": "must-not-export",
                },
            },
        )
        self.assertEqual(status, 200, body)
        published_preview = json.loads(body)
        self.assertEqual(published_preview["configPreviewError"], "")
        loaded = yaml.safe_load(published_preview["installConfig"])
        self.assertEqual(loaded["pullSecret"], secret)
        self.assertEqual(loaded["sshKey"].strip(), server.install_secret_status(destination)["sshPublicKey"])
        self.assertNotIn("<supplied at install>", published_preview["installConfig"])
        self.assertNotIn("dGVzdA==", published_preview["agentConfig"])
        install_file = server.ANSIBLE_WORKING_DIR / "cluster-install" / "install-config.yaml"
        agent_file = server.ANSIBLE_WORKING_DIR / "cluster-install" / "agent-config.yaml"
        self.assertEqual(published_preview["installConfigFile"], str(install_file))
        self.assertEqual(published_preview["agentConfigFile"], str(agent_file))
        self.assertEqual(published_preview["installFilesMessage"], "")
        self.assertEqual(install_file.read_text(), published_preview["installConfig"])
        self.assertEqual(agent_file.read_text(), published_preview["agentConfig"])
        self.assertEqual(stat.S_IMODE(install_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(agent_file.stat().st_mode), 0o644)
        written_install = yaml.safe_load(install_file.read_text())
        written_agent = yaml.safe_load(agent_file.read_text())
        self.assertEqual(written_install["pullSecret"], secret)
        self.assertEqual(written_install["sshKey"].strip(), loaded["sshKey"].strip())
        self.assertEqual(written_install["platform"], {"none": {}})
        self.assertEqual(written_install["controlPlane"]["replicas"], 1)
        self.assertEqual(written_install["compute"][0]["replicas"], 0)
        self.assertEqual(written_install["fips"], True)
        self.assertIn("edge.ocp.internal", written_install["imageDigestSources"][0]["mirrors"][0])
        self.assertEqual(written_agent["kind"], "AgentConfig")
        self.assertEqual(written_agent["rendezvousIP"], "192.0.2.11")
        self.assertEqual(written_agent["hosts"][0]["hostname"], "control-01")
        self.assertEqual(written_agent["hosts"][0]["role"], "master")
        self.assertEqual(
            written_agent["hosts"][0]["rootDeviceHints"]["serialNumber"],
            "NVME-C1",
        )
        self.assertEqual(
            written_agent["hosts"][0]["interfaces"][0]["macAddress"],
            "52:54:00:11:00:05",
        )
        self.assertEqual(
            written_agent["hosts"][0]["networkConfig"]["interfaces"][0]["name"],
            "eno5",
        )
        self.assertNotIn("dGVzdA==", agent_file.read_text())
        self.assertNotIn("PRIVATE KEY", install_file.read_text())
        self.assertNotIn("must-not-export", published_preview["installConfig"])
        output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
        published = "\n".join(
            path.read_text() for path in sorted(output_root.rglob("*")) if path.is_file()
        )
        self.assertNotIn("dGVzdA==", published)
        self.assertNotIn("ssh-ed25519", published)
        self.assertNotIn("PRIVATE KEY", published)
        self.assertNotIn("must-not-export", published)
        self.assertNotIn("pullSecret", published)
        self.assertFalse((output_root / "cluster-install").exists())
        status, body, _ = self.request(
            "GET",
            "/api/v1/install-secrets?outputDirectory=" + quote(destination),
        )
        self.assertEqual(status, 200, body)
        reported = json.loads(body)
        self.assertEqual(
            set(reported),
            {
                "pullSecretStored",
                "pullSecretBuiltIn",
                "pullSecretReady",
                "vaultPasswordAvailable",
                "privateKeyStored",
                "sshPublicKey",
            },
        )
        self.assertFalse(reported["pullSecretBuiltIn"])
        self.assertTrue(reported["pullSecretReady"])
        self.assertTrue(reported["vaultPasswordAvailable"])
        self.assertNotIn("dGVzdA==", body.decode())
        self.assertNotIn("PRIVATE KEY", body.decode())
        self.assertTrue(reported["pullSecretStored"])
        self.assertTrue(reported["sshPublicKey"].startswith("ssh-ed25519 "))
        database = self.database_bytes()
        self.assertNotIn(b"dGVzdA==", database)
        self.assertNotIn(b"ssh-ed25519", database)
        self.assertNotIn(b"PRIVATE KEY", database)
        status, body, _ = self.request("GET", "/api/v1/config")
        self.assertNotIn("pullSecret", json.loads(body)["blueprint"])
        status, body, _ = self.request(
            "GET",
            "/api/v1/install-secrets/ssh-private-key?outputDirectory=" + quote(destination),
        )
        self.assertEqual(status, 426)
        self.assertNotIn(b"PRIVATE KEY", body)
        status, body, _ = self.request(
            "POST",
            "/api/v1/ansible-files",
            {
                "outputDirectory": destination,
                "preferences": {
                    NODE_UUID: {
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
                "blueprint": {"controlPlaneTopology": "Compact"},
            },
        )
        self.assertEqual(status, 200, body)
        self.assertIn("Compact needs three nodes.", json.loads(body)["configPreviewError"])
        self.assertFalse(install_file.exists())
        self.assertFalse(agent_file.exists())
        self.assertTrue((install_file.parent / "pull-secret.vault").is_file())
        self.assertFalse((install_file.parent / "pull-secret.json").exists())
        self.assertTrue((install_file.parent / "id_ed25519.pub").is_file())

    def test_built_in_pull_secret_is_used_until_the_workspace_replaces_it(self):
        import yaml

        destination = str(server.ANSIBLE_WORKING_DIR)
        bundled = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
        replacement = '{"auths":{"override.example":{"auth":"b3ZlcnJpZGU="}}}'
        password = b"test-vault-password"
        bundled_path = Path(self.temp_dir.name) / "image-pull-secret.vault"
        bundled_path.write_text(server.format_pull_secret_vault(bundled, password), encoding="utf-8")
        self.assertNotIn("dGVzdA==", bundled_path.read_text())
        self._post_inspection(
            NODE_UUID,
            "NODE-SERIAL-01",
            [("eno5", "52:54:00:11:00:05")],
            [("/dev/nvme0n1", "NVME-C1")],
        )
        publish = {
            "outputDirectory": destination,
            "preferences": {
                NODE_UUID: {
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
        with patch.object(server, "BUNDLED_PULL_SECRET_FILE", bundled_path):
            self.assertEqual(server.bundled_pull_secret(), "")
            locked = server.install_secret_status(destination)
            self.assertTrue(locked["pullSecretBuiltIn"])
            self.assertFalse(locked["pullSecretReady"])
            self.assertFalse(locked["vaultPasswordAvailable"])
            self._vault_password(password)
            self.assertEqual(server.bundled_pull_secret(), bundled)
            created = server.generate_cluster_ssh_key(destination, replace=False)
            self.assertTrue(created["sshPublicKey"].startswith("ssh-ed25519 "))
            self.assertFalse(created["pullSecretStored"])
            self.assertTrue(created["pullSecretBuiltIn"])
            self.assertNotIn("dGVzdA==", json.dumps(created))
            secret_dir = server.ANSIBLE_WORKING_DIR / "cluster-install"
            self.assertFalse((secret_dir / "pull-secret.json").exists())
            status, body, _ = self.request("POST", "/api/v1/ansible-files", publish)
            self.assertEqual(status, 200, body)
            published = json.loads(body)
            self.assertEqual(published["configPreviewError"], "")
            self.assertEqual(published["installFilesMessage"], "")
            install_file = secret_dir / "install-config.yaml"
            self.assertEqual(yaml.safe_load(install_file.read_text())["pullSecret"], bundled)
            self.assertFalse((secret_dir / "pull-secret.json").exists())
            output_root = server.ANSIBLE_WORKING_DIR / "ocp-metal-ui"
            inventory_text = "\n".join(
                path.read_text() for path in sorted(output_root.rglob("*")) if path.is_file()
            )
            self.assertNotIn("dGVzdA==", inventory_text)
            self.assertNotIn(b"dGVzdA==", self.database_bytes())
            status, body, _ = self.request(
                "GET",
                "/api/v1/install-secrets?outputDirectory=" + quote(destination),
            )
            self.assertEqual(status, 200, body)
            reported = json.loads(body)
            self.assertTrue(reported["pullSecretBuiltIn"])
            self.assertFalse(reported["pullSecretStored"])
            self.assertNotIn("dGVzdA==", body.decode())
            saved = server.save_pull_secret(destination, replacement)
            self.assertTrue(saved["pullSecretStored"])
            self.assertTrue(saved["pullSecretBuiltIn"])
            self.assertNotIn("b3ZlcnJpZGU=", json.dumps(saved))
            status, body, _ = self.request("POST", "/api/v1/ansible-files", publish)
            self.assertEqual(status, 200, body)
            written = yaml.safe_load(install_file.read_text())
            self.assertEqual(written["pullSecret"], replacement)
            self.assertNotIn("dGVzdA==", install_file.read_text())
            self.assertIn("b3ZlcnJpZGU=", install_file.read_text())
            vault_file = secret_dir / "pull-secret.vault"
            self.assertTrue(vault_file.is_file())
            self.assertNotIn("dGVzdA==", vault_file.read_text())
            self.assertNotIn("b3ZlcnJpZGU=", vault_file.read_text())
            self.assertFalse((secret_dir / "pull-secret.json").exists())

    def test_pull_secret_vault_opens_with_ansible(self):
        import subprocess

        from ansible.parsing.vault import VaultLib, VaultSecret

        secret = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
        password = b"test-vault-password"
        text = server.format_pull_secret_vault(secret, password)
        self.assertNotIn("dGVzdA==", text)
        self.assertIn("ocp_pre_install_pull_secret_contents: !vault |", text)
        envelope = server.extract_vault_envelope(text)
        opened = VaultLib(secrets=[("default", VaultSecret(password))]).decrypt(envelope.encode())
        self.assertEqual(opened.decode(), secret)
        self.assertEqual(server.decrypt_vault_envelope(envelope, password), secret)
        with self.assertRaises(ValueError) as caught:
            server.decrypt_vault_envelope(envelope, b"wrong-password")
        self.assertIn("did not open", str(caught.exception))
        password_file = Path(self.temp_dir.name) / "pw"
        password_file.write_bytes(password + b"\n")
        completed = subprocess.run(
            [
                "ansible-vault",
                "encrypt_string",
                "--vault-password-file",
                str(password_file),
                "--stdin-name",
                "ocp_pre_install_pull_secret_contents",
            ],
            input=secret.encode(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        cli_text = completed.stdout.decode()
        self.assertNotIn("dGVzdA==", cli_text)
        self.assertEqual(
            server.normalize_pull_secret(
                server.decrypt_vault_envelope(server.extract_vault_envelope(cli_text), password)
            ),
            secret,
        )


    def test_pull_secret_rejection_compares_fragment_digests(self):
        digest = hmac.new(server._FRAGMENT_HMAC_KEY, b"testmark", hashlib.sha256).hexdigest()
        rejected = '{"auths":{"example.com":{"auth":"xxxtestmark"}}}'
        accepted = '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'
        with patch.object(server, "_REJECTED_FRAGMENT_MACS", frozenset({digest})):
            with self.assertRaises(ValueError) as caught:
                server.normalize_pull_secret(rejected)
            self.assertIn("not accepted", str(caught.exception))
            self.assertEqual(
                json.loads(server.normalize_pull_secret(accepted))["auths"]["example.com"]["auth"],
                "dGVzdA==",
            )

    def test_api_auth_scopes_host_and_fail_closed(self):
        destination = quote(str(server.ANSIBLE_WORKING_DIR))
        status, body, _ = self.request("GET", "/api/v1/health", token=None)
        self.assertEqual(status, 200, body)
        connection = http.client.HTTPConnection(*self.httpd.server_address)
        connection.request("GET", "/api/v1/health")
        health = connection.getresponse()
        self.assertIn("default-src 'self'", health.getheader("Content-Security-Policy", ""))
        health.read()
        connection.close()

        for method, path, payload in (
            ("GET", "/api/v1/config", None),
            ("GET", "/api/v1/nodes", None),
            ("GET", "/api/v1/not-a-route", None),
            ("GET", f"/api/v1/install-secrets?outputDirectory={destination}", None),
            ("POST", "/api/v1/nodes", {"ansibleHost": "blocked-01"}),
            ("PUT", "/api/v1/configuration", {"blueprint": {}, "preferences": {}}),
            ("DELETE", f"/api/v1/nodes/{NODE_UUID}", None),
        ):
            with self.subTest(method=method, path=path, token="absent"):
                status, body, _ = self.request(method, path, payload, token=None)
                self.assertEqual(status, 401, body)
                self.assertNotIn(self.api_token.encode(), body)
                self.assertNotIn(self.inspection_token.encode(), body)

        status, body, _ = self.request(
            "GET", "/api/v1/config", authorization=f"bearer {self.api_token}"
        )
        self.assertEqual(status, 200, body)
        status, body, _ = self.request(
            "GET", "/api/v1/nodes", authorization=f"BEARER {self.api_token}"
        )
        self.assertEqual(status, 200, body)
        status, body, _ = self.request(
            "GET", "/api/v1/config", authorization=f"Token {self.api_token}"
        )
        self.assertEqual(status, 401, body)

        status, body, _ = self.request(
            "GET", f"/api/v1/install-secrets?outputDirectory={destination}"
        )
        self.assertEqual(status, 200, body)
        reported = json.loads(body)
        self.assertIn("pullSecretStored", reported)
        self.assertIn("sshPublicKey", reported)
        status, body, _ = self.request(
            "POST",
            "/api/v1/install-secrets/pull-secret",
            {"outputDirectory": str(server.ANSIBLE_WORKING_DIR), "pullSecret": '{"auths":{"example.com":{"auth":"dGVzdA=="}}}'},
        )
        self.assertEqual(status, 426, body)
        status, body, _ = self.request(
            "POST",
            "/api/v1/install-secrets/ssh-key",
            {"outputDirectory": str(server.ANSIBLE_WORKING_DIR), "replace": False},
        )
        self.assertEqual(status, 426, body)
        for path, payload in (
            ("/api/v1/install-secrets/pull-secret", {"outputDirectory": str(server.ANSIBLE_WORKING_DIR), "pullSecret": "x"}),
            ("/api/v1/install-secrets/ssh-key", {"outputDirectory": str(server.ANSIBLE_WORKING_DIR), "replace": False}),
        ):
            status, body, _ = self.request(
                "POST", path, payload, token=self.inspection_token
            )
            self.assertEqual(status, 401, body)

        status, body, _ = self.request(
            "POST", "/api/v1/inspections", self.report(), token=self.inspection_token
        )
        self.assertEqual(status, 202, body)
        status, body, _ = self.request(
            "POST",
            f"/api/v1/inspections/{NODE_UUID}",
            self.report(),
            token=self.inspection_token,
        )
        self.assertEqual(status, 202, body)
        for method, path in (
            ("GET", "/api/v1/config"),
            ("GET", "/api/v1/nodes"),
            ("GET", f"/api/v1/install-secrets/ssh-private-key?outputDirectory={destination}"),
            ("POST", "/api/v1/ansible-files"),
        ):
            with self.subTest(inspection=path):
                payload = {"outputDirectory": str(server.ANSIBLE_WORKING_DIR)} if method == "POST" else None
                status, body, _ = self.request(method, path, payload, token=self.inspection_token)
                self.assertEqual(status, 401, body)

        missing = Path(self.temp_dir.name) / "missing-api-token"
        with patch.dict(
            os.environ,
            {"API_TOKEN": "", "API_TOKEN_FILE": str(missing)},
            clear=False,
        ):
            status, body, _ = self.request("GET", "/api/v1/config", token=None)
            self.assertEqual(status, 503, body)
            status, body, _ = self.request("POST", "/api/v1/inspections", self.report(), token=None)
            self.assertEqual(status, 503, body)
            status, _, _ = self.request("GET", "/api/v1/health", token=None)
            self.assertEqual(status, 200)

        status, body, _ = self.request("GET", "/api/v1/health", token=None, host="evil.example")
        self.assertEqual(status, 421, body)
        status, body, _ = self.request("GET", "/api/v1/config", host="evil.example")
        self.assertEqual(status, 421, body)
        status, body, _ = self.request("GET", "/api/v1/health", token=None, host="localhost")
        self.assertEqual(status, 200, body)
        status, _, _ = self.request("POST", "/api/v1/nodes", {"ansibleHost": "control-01"})
        self.assertEqual(status, 201)


class RedfishClientTests(unittest.TestCase):
    def test_retrieval_normalizes_inventory_and_logs_out(self):
        resources = {
            "/redfish/v1/": {
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
            },
            "/redfish/v1/Systems": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1"}]
            },
            "/redfish/v1/Systems/1": {
                "UUID": NODE_UUID,
                "SerialNumber": "RF-SERIAL-01",
                "Manufacturer": "Example",
                "Model": "Rack server",
                "ProcessorSummary": {"Count": 1, "Model": "Example CPU"},
                "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
                "MemorySummary": {"TotalSystemMemoryMiB": 4096},
                "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
                "EthernetInterfaces": {
                    "@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces"
                },
            },
            "/redfish/v1/Systems/1/Processors": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/Processors/1"}]
            },
            "/redfish/v1/Systems/1/Processors/1": {"Model": "Example CPU"},
            "/redfish/v1/Systems/1/Storage": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/Storage/1"}]
            },
            "/redfish/v1/Systems/1/Storage/1": {
                "Drives": {"@odata.id": "/redfish/v1/Systems/1/Drives"}
            },
            "/redfish/v1/Systems/1/Drives": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/Drives/1"}]
            },
            "/redfish/v1/Systems/1/Drives/1": {
                "SerialNumber": "DRIVE-01",
                "Model": "NVMe drive",
                "CapacityBytes": 1000,
            },
            "/redfish/v1/Systems/1/EthernetInterfaces": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/1"}]
            },
            "/redfish/v1/Systems/1/EthernetInterfaces/1": {
                "Name": "NIC 1",
                "MACAddress": "52:54:00:fa:19:bc",
                "LinkStatus": "LinkUp",
            },
        }
        client = server.RedfishClient("https://192.0.2.20")
        calls = []

        def fake_request(method, resource, body=None, headers=None):
            path = urlparse(resource).path
            calls.append((method, path, body, headers))
            if method == "POST":
                return {}, {
                    "x-auth-token": "session-token",
                    "location": "/redfish/v1/SessionService/Sessions/1",
                }
            if method == "DELETE":
                return {}, {}
            return resources[path], {}

        client.request = fake_request
        report = client.retrieve("operator", "temporary-password")
        self.assertEqual(report["uuid"], NODE_UUID)
        self.assertEqual(report["serial_number"], "RF-SERIAL-01")
        self.assertEqual(report["inventory"]["cpus"]["count"], 1)
        self.assertEqual(report["inventory"]["memory"]["total"], 4096 * 1024**2)
        self.assertEqual(report["inventory"]["disks"][0]["serial"], "DRIVE-01")
        self.assertEqual(
            report["inventory"]["interfaces"][0]["mac"], "52:54:00:fa:19:bc"
        )
        self.assertEqual(
            report["inventory"]["interfaces"][0]["link_status"], "LinkUp"
        )
        self.assertEqual(calls[-1][0:2], ("DELETE", "/redfish/v1/SessionService/Sessions/1"))
        self.assertEqual(calls[1][2], {"UserName": "operator", "Password": "temporary-password"})

    def test_missing_optional_collection_is_reported_as_warning(self):
        client = server.RedfishClient("https://192.0.2.20")
        warnings = []

        def missing(resource):
            raise server.RedfishError("not found", status=404)

        client.get = missing
        members = client.collection_members(
            {"@odata.id": "/redfish/v1/Systems/1/Memory"}, warnings, "Memory"
        )
        self.assertEqual(members, [])
        self.assertEqual(warnings, ["Memory resource was not found."])


if __name__ == "__main__":
    unittest.main()
