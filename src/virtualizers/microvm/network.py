"""One bridge, one subnet, one allocator: the host network every microVM shares.

This is the clearest reason the family layer exists rather than each backend
carrying its own copy. Host networking is a single resource. There is one bridge,
one guest subnet, one IP/MAC allocator that reads every VM's runtime state to
avoid handing the same address out twice, and one FORWARD chain the per-VM policy
is written into. Two backends allocating from stores they cannot see each other's
entries in would collide, and the collision would look like a guest that boots
and cannot be reached.

The guest-facing model, unchanged: a tap per VM enslaved to a host bridge, an
address derived from the ``vmachine_id`` rather than served by DHCP, and
guest-to-guest traffic routed through the host so the allow-list on the forward
hook actually sees it (see :func:`ensure_guest_l2_isolation`).

Configuration still lives under ``virtualizers.ch.*``. Those keys are a
user-visible surface -- they are in every installed node's ``config.yaml`` and in
the installer -- so renaming them is a separate change from moving the code that
reads them. See ``docs/BACKENDS.md``.
"""
import hashlib
import ipaddress
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.firewall import policy as fw_policy
from src.virtualizers.firewall import (
    TransportProtocol,
    allow_all_egress as vm_allow_all_egress,
    allow_connection_to_instance as vm_allow_connection_to_instance,
    allow_host_connection as vm_allow_host_connection,
    block_all as vm_block_all,
)
from src.virtualizers.microvm import serial
from src.virtualizers.microvm.errors import MicroVMError
from src.virtualizers.microvm.host import ensure_command_available, run
from src.virtualizers.microvm.runtime_state import list_runtime_states

env_manager = ConfigManager()
sc = SQLConnection()

NETWORK_MODE = env_manager.get("virtualizers.ch.NETWORK_MODE", "tap_bridge")
NETWORK_BRIDGE_NAME = env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch")
NETWORK_SUBNET = env_manager.get("virtualizers.ch.NETWORK_SUBNET", "192.168.200.0/24")
NETWORK_GATEWAY_IP = env_manager.get("virtualizers.ch.NETWORK_GATEWAY_IP", "192.168.200.1")
GUEST_NET_DEVICE = env_manager.get("virtualizers.ch.GUEST_NET_DEVICE", "auto")


def ip_network() -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(NETWORK_SUBNET, strict=False)
    except ValueError as e:
        raise MicroVMError(f"Invalid NETWORK_SUBNET: {NETWORK_SUBNET}") from e

    if network.version != 4:
        raise MicroVMError("Only IPv4 subnets are supported for microVM networking.")
    if network.num_addresses < 4:
        raise MicroVMError(f"NETWORK_SUBNET too small for VM allocation: {NETWORK_SUBNET}")
    return network


def used_ips() -> set[str]:
    """Every guest address this node has already committed.

    Both sources are needed and neither subsumes the other: the runtime states
    cover VMs the node has launched (including a foreign-backend one, and one
    still booting with no database row yet), and the database covers rows whose
    state file a teardown already removed.
    """
    used: set[str] = set()

    for state in list_runtime_states().values():
        ip = state.get("ip")
        if isinstance(ip, str) and ip:
            used.add(ip)

    for vmachine_id in sc.get_all_internal_containers_ids():
        ip = sc.get_internal_ip(id=vmachine_id)
        if ip:
            used.add(ip)

    return used


def deterministic_ip_and_mac(vmachine_id: str) -> Tuple[str, str]:
    """An address and MAC for this VM, derived from its id rather than served.

    No DHCP, so the address is stable for the life of the VM and the firewall can
    be written against it before the guest exists. Derived from the id and then
    probed against :func:`used_ips`, so two VMs never share one and a relaunch of
    the same id lands on the same address whenever it is free.
    """
    network = ip_network()
    gateway_ip = ipaddress.ip_address(NETWORK_GATEWAY_IP)
    if gateway_ip not in network:
        raise MicroVMError(
            f"NETWORK_GATEWAY_IP {NETWORK_GATEWAY_IP} does not belong to subnet {NETWORK_SUBNET}"
        )

    hosts = network.num_addresses - 2
    if hosts <= 1:
        raise MicroVMError(f"No usable host addresses in subnet {NETWORK_SUBNET}")

    used = used_ips()
    digest = hashlib.sha256(vmachine_id.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")

    selected_ip: Optional[str] = None
    for offset in range(hosts):
        host_index = ((seed + offset) % hosts) + 1
        candidate = ipaddress.ip_address(int(network.network_address) + host_index)
        candidate_str = str(candidate)
        if candidate == gateway_ip:
            continue
        if candidate_str in used:
            continue
        selected_ip = candidate_str
        break

    if not selected_ip:
        raise MicroVMError(f"No available IPs in subnet {NETWORK_SUBNET}")

    mac = f"02:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}:{digest[3]:02x}:{digest[4]:02x}"
    return selected_ip, mac


def ensure_guest_bridge() -> ipaddress.IPv4Network:
    """Create the guest bridge with its gateway address, idempotently. Root only.

    Split out of :func:`preflight` because the bridge is needed before any
    instance runs: it is the only vantage point from which the gateway port can be
    probed the way a guest reaches it (``src.utils.firewall.reachability``), and
    creating it lazily on the first launch meant that at every moment the node
    actually had to decide something about that port -- assigning it, verifying it
    at startup -- the bridge did not exist and nothing could be proven.

    Only the link, its address and its state. Forwarding, masquerading and guest
    isolation stay in :func:`preflight`: they are about carrying guest traffic,
    which is the virtualizer's business and not the gateway's.
    """
    network = ip_network()

    link_exists = run(["ip", "link", "show", NETWORK_BRIDGE_NAME], check=False)
    if link_exists.returncode != 0:
        run(["ip", "link", "add", NETWORK_BRIDGE_NAME, "type", "bridge"])

    addr_show = run(["ip", "-4", "addr", "show", "dev", NETWORK_BRIDGE_NAME], check=False)
    expected_cidr = f"{NETWORK_GATEWAY_IP}/{network.prefixlen}"
    if expected_cidr not in (addr_show.stdout or ""):
        run(["ip", "addr", "add", expected_cidr, "dev", NETWORK_BRIDGE_NAME])

    run(["ip", "link", "set", NETWORK_BRIDGE_NAME, "up"])
    return network


def preflight() -> ipaddress.IPv4Network:
    """Everything the host must be doing before a guest can be given a tap."""
    if NETWORK_MODE != "tap_bridge":
        raise MicroVMError(
            f"Unsupported NETWORK_MODE '{NETWORK_MODE}'. This phase supports only 'tap_bridge'."
        )

    for command in ("ip", "sysctl", "debugfs", "ping"):
        ensure_command_available(command)

    # Either netfilter front-end will do; the backend picks whichever this host
    # actually speaks, so demanding iptables specifically would fail an
    # nftables-only host that works perfectly well.
    if not any(shutil.which(command) for command in ("nft", "iptables")):
        raise MicroVMError(
            "No netfilter tool found in PATH: install nftables (preferred) or iptables."
        )

    network = ensure_guest_bridge()

    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    ensure_guest_l2_isolation()
    # Not fatal, on purpose. This is the one thing nodo writes into a table it does
    # not own, and a host that refuses it -- or an operator who turned it off -- has
    # a node that still boots guests. What must not happen is booting them while
    # claiming the network is fine: 'nodo doctor' settles that with a real packet.
    _ensure_forward_compat()
    ensure_masquerade(network)

    return network


def _ensure_forward_compat() -> None:
    """Ask for the guest paths back from whatever else filters this host's FORWARD.

    See ``src/utils/firewall/compat.py`` for what this does and, just as important,
    what it cannot reach -- firewalld's own table is out of its range, and saying so
    is better than a rule that looks like it should have worked.
    """
    from src.virtualizers.firewall import ensure_forward_compat

    try:
        state = ensure_forward_compat(NETWORK_BRIDGE_NAME)
    except Exception as e:
        log.LOGGER(f"[FW] Could not evaluate the forward compatibility rules: {e}")
        return
    if state.error:
        log.LOGGER(f"[FW] {state.detail} {state.error}")


def ensure_guest_l2_isolation() -> None:
    """Route guest-to-guest traffic through the host, where the policy lives.

    ``block_all`` and every ``allow`` nodo writes sit on the *forward* hook, which
    only sees packets the host routes. Two guests on this bridge share one L2
    domain: they ARP each other directly and their frames are switched tap to
    tap, never reaching that hook. So the allow-list is a no-op for the one class
    of destination that matters most -- the other instances on this node.

    Isolating the ports (see :func:`create_tap`) alone only breaks things: the
    neighbour stops answering ARP, so a service cannot reach its own dependency
    either. Proxy ARP is the other half. The host answers on the neighbour's
    behalf, the guest hands it the frame, and the host routes it -- which is
    exactly what puts the packet in front of the forward chain. ``proxy_arp_pvlan``
    is the variant that replies on the interface the request arrived on; plain
    ``proxy_arp`` stays silent in that case, which is the case we have. And
    redirects have to go: otherwise the host helpfully tells the guest to talk to
    the neighbour directly, which isolation has just made impossible.

    Failing here is deliberate. A half-applied setup is the worst outcome: either
    guests reach each other unfiltered while the rules claim otherwise, or
    dependencies break with no ARP answer and no route.
    """
    for key, value in (
        (f"net.ipv4.conf.{NETWORK_BRIDGE_NAME}.proxy_arp", "1"),
        (f"net.ipv4.conf.{NETWORK_BRIDGE_NAME}.proxy_arp_pvlan", "1"),
        (f"net.ipv4.conf.{NETWORK_BRIDGE_NAME}.send_redirects", "0"),
    ):
        run(["sysctl", "-w", f"{key}={value}"])


def ensure_masquerade(network: ipaddress.IPv4Network) -> None:
    """Source-NAT the guest subnet on its way off this host.

    Shared by every VM in NETWORK_SUBNET, so it is never part of a single
    instance's teardown: removing it would cut connectivity for the others.
    """
    from src.virtualizers.microvm.firewall import backend

    rule = fw_policy.masquerade_rule(network.with_prefixlen)
    try:
        if backend().ensure(rule):
            log.LOGGER(f"[FW] Masquerading {network.with_prefixlen} on egress.")
    except Exception as e:
        raise MicroVMError(f"Could not ensure the guest subnet masquerade: {e}") from e


def create_tap(vmachine_id: str) -> str:
    tap_suffix = hashlib.sha1(vmachine_id.encode("utf-8")).hexdigest()[:10]
    tap_name = f"tap{tap_suffix}"

    # Check if TAP already exists and delete it to avoid conflicts
    if run(["ip", "link", "show", "dev", tap_name], check=False).returncode == 0:
        run(["ip", "link", "del", tap_name], check=False)

    run(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"])
    run(["ip", "link", "set", tap_name, "master", NETWORK_BRIDGE_NAME])
    # An isolated bridge port can only exchange frames with the bridge itself,
    # never with another isolated port, so a guest reaches its neighbours through
    # the host -- and through the firewall. See ensure_guest_l2_isolation for the
    # proxy-ARP half this depends on.
    run(["ip", "link", "set", "dev", tap_name, "type", "bridge_slave", "isolated", "on"])
    run(["ip", "link", "set", tap_name, "up"])

    return tap_name


def delete_tap(tap_name: str) -> None:
    run(["ip", "link", "del", tap_name], check=False)


def add_dnat_rule(
    vmachine_id: str, protocol: str, external_port: int, vm_ip: str, internal_port: int
) -> None:
    """Publish a guest port on the host.

    PREROUTING does the translation; the FORWARD pair lets the translated packet
    and its replies past the guest policy. OUTPUT is not involved because it only
    sees traffic the host itself originates.

    Nothing is returned: the rules carry this VM's comment prefix, so teardown
    deletes them by prefix instead of replaying the arguments that created them.
    """
    from src.virtualizers.microvm.firewall import backend

    active = backend()
    for rule in fw_policy.port_forward_rules(
        vmachine_id=vmachine_id,
        vm_ip=vm_ip,
        protocol=protocol,
        external_port=external_port,
        internal_port=internal_port,
    ):
        try:
            active.ensure(rule)
        except Exception as e:
            raise MicroVMError(
                f"Could not publish {protocol} port {external_port} for {vmachine_id}: {e}"
            ) from e


def replay_legacy_cleanup_rules(
    commands: List[List[str]], log_prefix: str = ""
) -> None:
    """Replay the removal commands stored by pre-nftables versions of nodo.

    Kept for runtime state written before rules were deleted by comment. New
    instances persist an empty list and are torn down by comment prefix instead,
    so for anything this version launched the list is empty and this is a no-op.

    ``log_prefix`` turns each attempt into a log line, which teardown wants and
    the launcher's own failure path does not -- there the exception being handled
    is the thing worth reading.
    """
    for command in commands:
        try:
            run(command, check=False)
            if log_prefix:
                log.LOGGER(f"{log_prefix} cleanup DNAT rule attempted: {command}")
        except Exception as e:
            if log_prefix:
                log.LOGGER(f"{log_prefix} error cleaning DNAT rule {command}: {e}")


def log_host_network_probe(log_prefix: str, vm_ip: str, tap_name: Optional[str]) -> None:
    probes: List[Tuple[str, List[str]]] = [
        ("bridge_addr", ["ip", "-4", "addr", "show", "dev", NETWORK_BRIDGE_NAME]),
        ("route_to_vm", ["ip", "-4", "route", "get", vm_ip]),
        ("neigh_vm", ["ip", "-4", "neigh", "show", vm_ip]),
    ]
    if tap_name:
        probes.append(("tap_link", ["ip", "link", "show", tap_name]))

    for label, command in probes:
        result = run(command, check=False)
        stdout = (result.stdout or "").strip() or "<empty>"
        stderr = (result.stderr or "").strip() or "<empty>"
        log.LOGGER(
            f"{log_prefix} host network probe {label}: rc={result.returncode}, "
            f"stdout={stdout}, stderr={stderr}"
        )


def neighbor_is_usable(neigh_output: str) -> bool:
    text = neigh_output.strip().upper()
    if not text:
        return False
    if "FAILED" in text or "INCOMPLETE" in text:
        return False
    if "REACHABLE" in text or "STALE" in text or "DELAY" in text or "PROBE" in text or "PERMANENT" in text:
        return True
    return "LLADDR" in text


def ready_timeout_seconds(raw_timeout) -> float:
    """Validate a configured readiness timeout.

    Taken as an argument because the two hypervisors need different windows for
    the same guest: a KVM boot answers in a second, and the same image under TCG
    emulation takes an order of magnitude longer. Both must be positive numbers,
    and a typo has to fail here rather than turn into an immediate timeout that
    reads as a broken guest.
    """
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as e:
        raise MicroVMError(
            f"Invalid GUEST_NETWORK_READY_TIMEOUT_S value: {raw_timeout!r}"
        ) from e

    if timeout <= 0:
        raise MicroVMError(
            f"GUEST_NETWORK_READY_TIMEOUT_S must be > 0. Got: {raw_timeout!r}"
        )
    return timeout


def wait_guest_network_ready(
    log_prefix: str,
    vm_ip: str,
    timeout_s: float,
    serial_log_path: Optional[Path],
) -> None:
    """Block until the guest answers on the network, or say why it never will.

    The serial log is polled alongside the ping because a guest whose initramfs
    gave up will never answer, and waiting out the whole timeout for it reports a
    timeout instead of the reason (see ``serial.detect_initramfs_fatal``).
    """
    deadline = time.monotonic() + timeout_s
    attempt = 0
    last_ping_stdout = "<empty>"
    last_ping_stderr = "<empty>"
    last_neigh_stdout = "<empty>"
    last_ping_rc: Optional[int] = None

    while time.monotonic() < deadline:
        attempt += 1
        initramfs_fatal = serial.detect_initramfs_fatal(serial_log_path=serial_log_path)
        if initramfs_fatal:
            raise MicroVMError(
                f"Guest initramfs boot error before network ready: {initramfs_fatal}"
            )

        ping_result = run(["ping", "-4", "-c", "1", "-W", "1", vm_ip], check=False)
        neigh_result = run(["ip", "-4", "neigh", "show", vm_ip], check=False)

        last_ping_rc = ping_result.returncode
        last_ping_stdout = (ping_result.stdout or "").strip() or "<empty>"
        last_ping_stderr = (ping_result.stderr or "").strip() or "<empty>"
        last_neigh_stdout = (neigh_result.stdout or "").strip() or "<empty>"

        ready = ping_result.returncode == 0 or neighbor_is_usable(last_neigh_stdout)
        log.LOGGER(
            f"{log_prefix} guest network readiness attempt={attempt}, "
            f"ping_rc={ping_result.returncode}, neigh={last_neigh_stdout}"
        )
        if ready:
            log.LOGGER(
                f"{log_prefix} guest network readiness passed after {attempt} attempt(s)"
            )
            return

        time.sleep(0.5)

    initramfs_fatal = serial.detect_initramfs_fatal(serial_log_path=serial_log_path)
    if initramfs_fatal:
        raise MicroVMError(
            f"Guest initramfs boot error before network ready: {initramfs_fatal}"
        )

    raise MicroVMError(
        f"Guest network did not become ready within {timeout_s:.1f}s for {vm_ip}. "
        f"last_ping_rc={last_ping_rc}, last_ping_stdout={last_ping_stdout}, "
        f"last_ping_stderr={last_ping_stderr}, last_neigh={last_neigh_stdout}. "
        "Check guest serial log for initramfs network bootstrap errors and verify "
        "custom initramfs/rootfs entrypoint."
    )


def guest_ip_cmdline_token(vm_ip: str, netmask: str) -> str:
    """The kernel ``ip=`` autoconfiguration token for a guest on this bridge.

    Shared because the addressing is: same bridge, same gateway, same netmask,
    whichever hypervisor boots the guest. What differs is the ``console=`` beside
    it, which is architecture-determined and stays with the backend that builds
    the cmdline.
    """
    guest_dev = str(GUEST_NET_DEVICE).strip() if GUEST_NET_DEVICE is not None else ""
    if not guest_dev or guest_dev.lower() in {"auto", "none"}:
        # Leave the kernel interface field empty so it selects the first usable NIC.
        return f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}:::off"
    return f"ip={vm_ip}::{NETWORK_GATEWAY_IP}:{netmask}::{guest_dev}:off"


# Name resolution is deliberately not nodo's business.
#
# This module used to resolve every network tag that looked like a domain and write
# the answers into the guest's own /etc/hosts, plus an /etc/resolv.conf naming the
# bridge address as the guest's nameserver. Both are gone, because both were wrong
# in the same way: the node reached into a filesystem it does not own to install a
# glibc-specific convention that the service could neither declare, refuse, nor
# receive in a format of its choosing -- which is precisely what
# `Container.ConfigDeclaration` exists to avoid.
#
# What the node owes a guest is the DATA, and it already delivers it the declared
# way: `ConfigurationFile.network_resolution` (tags -> peer Instances, each with its
# uri_slot and protocol stack) inside `__config__`, at the path and format the
# service declared. That is strictly richer than a hosts file -- it carries ports,
# protocols and env-filtered peers, and it is not frozen at launch the way a
# resolved A record is.
#
# Name resolution on top of that is a service's job, and the ecosystem already does
# it: a service reads `network_resolution` from its own `__config__` and serves DNS
# from it for whatever inside the guest wants `getaddrinfo`. The injected
# resolv.conf actively broke that -- it pointed the guest at the bridge address,
# where nodo listens for gRPC and nothing answers on 53, instead of leaving the
# guest's own resolver reachable.
#
# A `Network` in the spec is "a logical communication domain", identified by tags.
# Reading a tag as a DNS hostname was nodo's invention, not the model's.


def configure_guest_firewall_policy(
    log_prefix: str,
    vmachine_id: str,
    vm_ip: str,
    network_resolution: List[celaut.ConfigurationFile.NetworkResolution],
) -> None:
    if not vm_block_all(vmachine_id=vmachine_id, source_ip=vm_ip):
        raise MicroVMError(
            f"Failed to apply default deny firewall policy for VM {vmachine_id} ({vm_ip})."
        )

    # NETWORK_GATEWAY_IP is one of this host's own addresses, so this allow goes on
    # the *input* hook. Written as an ordinary egress allow -- which is what it was
    # -- it landed in FORWARD, which a packet addressed to the host never traverses:
    # the rule could not match anything, while the log announced it as granted
    # access. See `policy.allow_host_connection_rule`.
    #
    # The gateway is the only service of the node's own that a guest is given access
    # to. There is no rule for port 53: nodo does not serve DNS, and a guest that wants
    # name resolution gets it from a service (see the note above), reached through the
    # ordinary peer-instance allows or inside its own container.
    #
    # Both of the gateway's ports are opened: the plaintext one is what this guest's
    # __config__ names (a service speaks plain gRPC), and the TLS one is reachable too,
    # so a service that wants to pin the node's certificate can (issue #257).
    #
    # Both are read here rather than at import: they are assigned by the daemon, which
    # may well happen after this module was first loaded.
    for gateway_port in dict.fromkeys(
        port
        for port in (
            env_manager.get_plaintext_gateway_port(),
            env_manager.get_gateway_port(),
        )
        if port
    ):
        if not vm_allow_host_connection(
            vmachine_id=vmachine_id,
            host_ip=NETWORK_GATEWAY_IP,
            port=gateway_port,
            protocol=TransportProtocol.TCP,
            source_ip=vm_ip,
        ):
            raise MicroVMError(
                f"Failed to allow gateway access for VM {vmachine_id}: "
                f"{vm_ip} -> {NETWORK_GATEWAY_IP}:{gateway_port}/tcp"
            )
        log.LOGGER(
            f"{log_prefix} firewall allow gateway: "
            f"{vm_ip} -> {NETWORK_GATEWAY_IP}:{gateway_port}/tcp"
        )

    # Network tag "*" => open-internet egress. Allow-all is inserted at the head
    # of FORWARD so it takes precedence over the default-deny block_all rule.
    if any(tag == "*" for net_res in network_resolution for tag in net_res.tags):
        if not vm_allow_all_egress(vmachine_id=vmachine_id, source_ip=vm_ip):
            raise MicroVMError(
                f"Failed to apply allow-all egress (network tag '*') for VM {vmachine_id} ({vm_ip})."
            )
        log.LOGGER(f"{log_prefix} firewall allow-all egress (network tag '*')")

    for net_res in network_resolution:
        tag = net_res.tags[0] if net_res.tags else "<untagged>"
        rule_applied = False
        for instance in net_res.peer_instances:
            if vm_allow_connection_to_instance(
                vmachine_id=vmachine_id,
                instance=instance,
                source_ip=vm_ip,
            ):
                log.LOGGER(
                    f"{log_prefix} firewall allow network tag '{tag}' resolved via peer instance"
                )
                rule_applied = True
                break

        if not rule_applied:
            log.LOGGER(
                f"{log_prefix} firewall warning: no egress rule could be applied for network tag '{tag}'"
            )
