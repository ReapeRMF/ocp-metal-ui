"""Build agent-config and install-config facts from the UI inventory.

The UI keeps SQLite. The publish step writes the same facts into
group_vars/ocp.yml and host_vars. This filter reads those published
facts and returns the host records the install templates print.
"""


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _enabled(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _ipv4(value):
    text = _text(value).split("/", 1)[0].strip()
    parts = text.split(".")
    if len(parts) != 4:
        return ""
    numbers = []
    for part in parts:
        if not part.isdigit():
            return ""
        number = int(part)
        if number > 255:
            return ""
        numbers.append(str(number))
    return ".".join(numbers)


def _last_octet(value):
    if isinstance(value, bool) or value is None:
        return ""
    text = _text(value)
    if not text.isdigit():
        return ""
    number = int(text)
    if number > 255:
        return ""
    return str(number)


def _parse_cidr(cidr):
    text = _text(cidr)
    if "/" not in text:
        raise ValueError("machineNetworkCidr must look like 205.28.113.0/24.")
    address, prefix_text = text.split("/", 1)
    address = _ipv4(address)
    if not prefix_text.isdigit():
        raise ValueError("machineNetworkCidr must look like 205.28.113.0/24.")
    prefix = int(prefix_text)
    if not address or prefix < 1 or prefix > 32:
        raise ValueError("machineNetworkCidr must be an IPv4 prefix from /1 to /32.")
    return address, prefix


def _in_network(address, network, prefix):
    def pack(ip):
        parts = [int(part) for part in ip.split(".")]
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]

    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (pack(address) & mask) == (pack(network) & mask)


def _address_from_last_octet(octet, network):
    parts = network.split(".")
    if len(parts) != 4:
        return ""
    return ".".join(parts[:-1] + [octet])


def _interfaces(source):
    if not isinstance(source, dict):
        return []
    raw = source.get("interfaces")
    if not isinstance(raw, list):
        raw = source.get("nics")
    if not isinstance(raw, list):
        return []
    interfaces = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("ifname"))
        mac = _text(item.get("mac_address") or item.get("mac") or item.get("macAddress"))
        if name and mac and mac.lower() != "mac unavailable":
            interfaces.append({"name": name, "mac": mac.lower()})
    return interfaces


def _inventory(variables):
    for key in ("ipa_inventory", "redfish_inventory"):
        source = variables.get(key)
        if isinstance(source, dict) and source:
            return source
    return {}


def _selected_nics(preferences):
    preferences = preferences if isinstance(preferences, dict) else {}
    if preferences.get("netType") == "bond":
        names = preferences.get("bondNics") or []
        if not isinstance(names, list):
            return []
        selected = []
        for name in names:
            text = _text(name)
            if text and text not in selected:
                selected.append(text)
        return selected
    name = _text(preferences.get("singleNic"))
    return [name] if name else []


def _vlan_id(preferences):
    preferences = preferences if isinstance(preferences, dict) else {}
    if preferences.get("netType") == "bond":
        enabled = _enabled(preferences.get("bondVlanEnabled"))
        raw = preferences.get("bondVlanId")
    else:
        enabled = _enabled(preferences.get("singleVlanEnabled"))
        raw = preferences.get("singleVlanId")
    if not enabled:
        return None
    text = _text(raw)
    if not text.isdigit():
        raise ValueError("VLAN ID must be a number from 1 to 4094.")
    vlan_id = int(text)
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError("VLAN ID must be a number from 1 to 4094.")
    return vlan_id


def _host_address(variables, network):
    explicit = _text(variables.get("ipv4_ip"))
    if explicit:
        address = _ipv4(explicit)
        if not address:
            raise ValueError("ipv4_ip must be an IPv4 address.")
        return address
    octet = _last_octet(variables.get("ipv4LastOctet"))
    if _text(variables.get("ipv4LastOctet")) and not octet:
        raise ValueError("ipv4LastOctet must be 0 through 255.")
    if octet and network:
        return _address_from_last_octet(octet, network)
    return _ipv4(_inventory(variables).get("current_ip"))


def _ethernet(name):
    return {
        "name": name,
        "type": "ethernet",
        "state": "up",
        "ipv4": {"enabled": False},
        "ipv6": {"enabled": False},
    }


def _with_address(iface, address, prefix):
    iface["ipv4"] = {
        "enabled": True,
        "dhcp": False,
        "address": [{"ip": address, "prefix-length": prefix}],
    }
    iface["ipv6"] = {"enabled": False}
    return iface


def _bond(members):
    return {
        "name": "bond0",
        "type": "bond",
        "state": "up",
        "link-aggregation": {
            "mode": "active-backup",
            "options": {"miimon": "100"},
            "port": members,
        },
        "ipv4": {"enabled": False},
        "ipv6": {"enabled": False},
    }


def _vlan(base_name, vlan_id):
    return {
        "name": f"{base_name}.{vlan_id}",
        "type": "vlan",
        "state": "up",
        "vlan": {"base-iface": base_name, "id": vlan_id},
        "ipv4": {"enabled": False},
        "ipv6": {"enabled": False},
    }


def _network_config(nic_names, vlan_id, address, prefix, gateway, dns_servers):
    interfaces = [_ethernet(name) for name in nic_names]
    bonded = len(nic_names) > 1
    if bonded:
        interfaces.append(_bond(nic_names))
        base_name = "bond0"
    else:
        base_name = nic_names[0]
    if vlan_id is None:
        l3 = interfaces[-1]
        l3_name = base_name
    else:
        l3 = _vlan(base_name, vlan_id)
        interfaces.append(l3)
        l3_name = l3["name"]
    _with_address(l3, address, prefix)
    config = {"interfaces": interfaces}
    if dns_servers:
        config["dns-resolver"] = {"config": {"server": dns_servers}}
    if gateway:
        config["routes"] = {
            "config": [
                {
                    "destination": "0.0.0.0/0",
                    "next-hop-address": gateway,
                    "next-hop-interface": l3_name,
                }
            ]
        }
    return config


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        return []
    results = []
    for item in items:
        text = _text(item)
        if text and text not in results:
            results.append(text)
    return results


_ROLE_DUTIES = ("master", "worker", "storage")
_TOPOLOGY_LABELS = {
    "Compact": "Compact",
    "HighlyAvailable": "Highly Available",
    "SingleNode": "Single node",
}


def _duties(variables):
    raw = variables.get("roles")
    names = []
    if isinstance(raw, list):
        names = raw
    elif isinstance(raw, str) and raw.strip():
        names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        role = _text(variables.get("role"))
        names = [role] if role else []
    duties = []
    for name in names:
        text = _text(name)
        if text not in _ROLE_DUTIES:
            raise ValueError("role must be master, worker, or storage.")
        if text not in duties:
            duties.append(text)
    return [name for name in _ROLE_DUTIES if name in duties]


def _fail(message):
    return {"ok": False, "error": message, "hosts": []}


def agent_hosts(plan):
    """Return install facts for one published UI inventory.

    plan.blueprint is the ocp group vars. plan.hosts is the ocp group,
    each item shaped as {"name", "variables"}.
    """
    if not isinstance(plan, dict):
        return _fail("The install plan must be an object.")
    blueprint = plan.get("blueprint") if isinstance(plan.get("blueprint"), dict) else {}
    hosts = plan.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return _fail("The inventory has no hosts in the ocp group.")

    cluster_name = _text(blueprint.get("clusterName"))
    base_domain = _text(blueprint.get("baseDomain"))
    topology = _text(blueprint.get("controlPlaneTopology"))
    if not cluster_name or not base_domain:
        return _fail("group_vars/ocp.yml needs clusterName and baseDomain.")
    if topology not in _TOPOLOGY_LABELS:
        return _fail("controlPlaneTopology must be Compact, HighlyAvailable, or SingleNode.")
    try:
        network, prefix = _parse_cidr(blueprint.get("machineNetworkCidr"))
    except ValueError as error:
        return _fail(str(error))

    gateway = _ipv4(blueprint.get("networkGateway"))
    if _text(blueprint.get("networkGateway")) and not gateway:
        return _fail("networkGateway must be an IPv4 address.")
    if gateway and not _in_network(gateway, network, prefix):
        return _fail("networkGateway is outside the machine network.")
    dns_servers = []
    for server in _string_list(blueprint.get("dnsServers")):
        address = _ipv4(server)
        if not address:
            return _fail(f"DNS server {server} must be an IPv4 address.")
        dns_servers.append(address)
    ntp_sources = _string_list(blueprint.get("ntpServers"))

    built = []
    for host in hosts:
        if not isinstance(host, dict):
            return _fail("Each ocp host entry must be an object.")
        name = _text(host.get("name"))
        variables = host.get("variables") if isinstance(host.get("variables"), dict) else {}
        if not name:
            return _fail("An ocp host is missing its inventory name.")
        if variables.get("node_status") == "awaiting_ipa":
            return _fail(f"{name} is still waiting for hardware.")
        try:
            duties = _duties(variables)
        except ValueError as error:
            return _fail(f"{name}: {error}")
        if "master" in duties:
            role = "master"
        elif "worker" in duties:
            role = "worker"
        else:
            return _fail(
                f"{name} needs a master or worker role. Storage is recorded with those roles."
            )
        preferences = variables.get("node_preferences")
        if not isinstance(preferences, dict):
            preferences = {}
        nic_names = _selected_nics(preferences)
        if preferences.get("netType") == "bond" and len(nic_names) < 2:
            return _fail(f"{name} needs at least two NICs on bond0.")
        if not nic_names:
            return _fail(f"{name} needs a machine-network NIC.")
        try:
            vlan_id = _vlan_id(preferences)
            address = _host_address(variables, network)
        except ValueError as error:
            return _fail(f"{name}: {error}")
        if not address:
            return _fail(f"{name} has no machine-network address.")
        if not _in_network(address, network, prefix):
            return _fail(f"{name} address {address} is outside the machine network.")
        serial = _text(preferences.get("rootDisk"))
        if not serial:
            return _fail(f"{name} needs an install disk serial.")
        known = {item["name"]: item["mac"] for item in _interfaces(_inventory(variables))}
        agent_interfaces = []
        for nic_name in nic_names:
            mac = known.get(nic_name, "")
            if not mac:
                return _fail(f"{name} NIC {nic_name} was not in the hardware inventory.")
            agent_interfaces.append({"name": nic_name, "macAddress": mac})
        built.append(
            {
                "hostname": name,
                "role": role,
                "duties": duties,
                "rendezvous": _enabled(variables.get("rendezvous")),
                "address": address,
                "interfaces": agent_interfaces,
                "root_device_hints": {"serialNumber": serial},
                "network_config": _network_config(
                    nic_names, vlan_id, address, prefix, gateway, dns_servers
                ),
            }
        )

    rendezvous = [host for host in built if host["rendezvous"]]
    if len(rendezvous) != 1:
        return _fail("Exactly one master must be the rendezvous host.")
    if rendezvous[0]["role"] != "master":
        return _fail("The rendezvous host must be a master.")
    masters = [host for host in built if host["role"] == "master"]
    workers = [host for host in built if host["role"] == "worker"]
    if topology == "Compact":
        if len(built) != 3:
            return _fail("Compact needs three nodes.")
        for host in built:
            if not {"master", "worker", "storage"} <= set(host["duties"]):
                return _fail(
                    f"{host['hostname']} needs master, worker, and storage roles."
                )
    if topology == "HighlyAvailable" and len(masters) != 3:
        return _fail("Highly Available needs three masters.")
    if topology == "SingleNode" and (len(masters) != 1 or workers):
        return _fail("Single node needs one master and no dedicated workers.")

    platform = "none" if topology == "SingleNode" else "baremetal"
    api_vips = []
    ingress_vips = []
    if platform == "baremetal":
        api_vip = _ipv4(blueprint.get("apiVip"))
        ingress_vip = _ipv4(blueprint.get("ingressVip"))
        if _text(blueprint.get("apiVip")) and not api_vip:
            return _fail("apiVip must be an IPv4 address.")
        if _text(blueprint.get("ingressVip")) and not ingress_vip:
            return _fail("ingressVip must be an IPv4 address.")
        if not api_vip or not ingress_vip:
            return _fail(
                f"{_TOPOLOGY_LABELS[topology]} needs apiVip and ingressVip in group_vars/ocp.yml."
            )
        for label, vip in (("apiVip", api_vip), ("ingressVip", ingress_vip)):
            if not _in_network(vip, network, prefix):
                return _fail(f"{label} is outside the machine network.")
            if any(host["address"] == vip for host in built):
                return _fail(f"{label} is the same address as a node.")
        if api_vip == ingress_vip:
            return _fail("apiVip and ingressVip must be different addresses.")
        api_vips = [api_vip]
        ingress_vips = [ingress_vip]

    return {
        "ok": True,
        "error": "",
        "cluster_name": cluster_name,
        "base_domain": base_domain,
        "edge_fqdn": f"edge.{base_domain}",
        "machine_network_cidr": f"{network}/{prefix}",
        "platform": platform,
        "api_vips": api_vips,
        "ingress_vips": ingress_vips,
        "master_replicas": len(masters),
        "worker_replicas": len(workers),
        "ntp_sources": ntp_sources,
        "rendezvous_ip": rendezvous[0]["address"],
        "ca_cert_bundle": blueprint.get("caCertBundle") if isinstance(blueprint.get("caCertBundle"), str) else "",
        "hosts": built,
    }


class FilterModule:
    def filters(self):
        return {"agent_hosts": agent_hosts}
