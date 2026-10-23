import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import server


NODE_UUID = "8a4f91c2-38d1-4bda-912c-bbfa47821001"


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = server.DATABASE_PATH
        self.original_ansible_working_dir = server.ANSIBLE_WORKING_DIR
        server.DATABASE_PATH = Path(self.temp_dir.name) / "inventory.sqlite3"
        server.ANSIBLE_WORKING_DIR = Path(self.temp_dir.name) / "ansible"
        server.ANSIBLE_WORKING_DIR.mkdir()
        server.initialize_database()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        server.DATABASE_PATH = self.original_database_path
        server.ANSIBLE_WORKING_DIR = self.original_ansible_working_dir
        self.temp_dir.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.read()
        content_type = response.getheader("Content-Type", "")
        connection.close()
        return response.status, result, content_type

    def report(self, **overrides):
        payload = {
            "uuid": NODE_UUID,
            "bmc_address": "10.10.50.21",
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

    def test_inspection_is_read_from_sqlite_and_preferences_survive_reports(self):
        status, _, _ = self.request("POST", "/api/v1/inspections", self.report())
        self.assertEqual(status, 202)
        status, body, _ = self.request(
            "PUT",
            "/api/v1/nodes/preferences",
            {"preferences": {NODE_UUID: {"rootDisk": "/dev/sda", "netType": "single"}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"saved": 1})

        updated_report = self.report(bmc_address="10.10.50.99")
        status, _, _ = self.request("POST", f"/api/v1/inspections/{NODE_UUID}", updated_report)
        self.assertEqual(status, 202)
        status, body, _ = self.request("GET", "/api/v1/nodes")
        self.assertEqual(status, 200)
        node = json.loads(body)[0]
        self.assertEqual(node["bmc_address"], "10.10.50.99")
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
                "group_vars/baremetal.yml",
                "host_vars/server-01.yml",
            },
        )
        inventory = (output_root / "inventory.yml").read_text()
        group_vars = (output_root / "group_vars/baremetal.yml").read_text()
        host_vars = (output_root / "host_vars/server-01.yml").read_text()
        self.assertIn('"baremetal"', inventory)
        self.assertIn('"server-01"', inventory)
        self.assertIn('"serial_number": "NODE-SERIAL-01"', inventory)
        self.assertIn('"cluster: #1"', group_vars)
        self.assertIn('"machineNetworkCidr": "10.20.0.0/16"', group_vars)
        self.assertIn('"dnsServers":\n    - "10.20.0.53"', group_vars)
        self.assertIn('"ntpServers":\n    - "ntp.example.com"', group_vars)
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
        group_vars = (output_root / "group_vars/baremetal.yml").read_text()
        self.assertIn('"control-01": {}', inventory)
        self.assertIn('"node_status": "awaiting_ipa"', host_vars)
        self.assertIn('"clusterName": "new-cluster"', group_vars)

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

        database_contents = server.DATABASE_PATH.read_bytes()
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
        self.assertNotIn(b"transient-secret", server.DATABASE_PATH.read_bytes())

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
        self.assertIn('"baremetal"', inventory)
        self.assertTrue((output_root / "group_vars/baremetal.yml").is_file())


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
