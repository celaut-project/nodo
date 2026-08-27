"""Per-VM network policy for the microVM virtualizers (cloud-hypervisor, qemu).

A thin adapter: resolve which address a VM has, ask ``utils.firewall.policy``
which rules that implies, and apply them through whichever netfilter backend the
host speaks. The rules themselves are decided in that pure module so they can be
tested without a node -- this file only does the parts that need the database and
the config.

Rules go through the backend rather than raw ``iptables`` so that everything nodo
writes lives in one place. On an nftables host that also makes the blanket drop
stronger: ``drop`` is terminal for the whole hook, so a guest's isolation now runs
ahead of anything a host firewall might accept.
"""

from typing import List, Optional

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.firewall import policy
from src.utils.firewall.backends import FirewallBackend, FirewallError, detect_backend
from src.utils.firewall.rules import Chain, Rule
from src.utils.logger import LOGGER as logger
from src.virtualizers.ch.runtime_state import load_runtime_state
from src.virtualizers.firewall import TransportProtocol, resolve_slot_transport_protocols

sc = SQLConnection()

_BACKEND: Optional[FirewallBackend] = None


def backend() -> FirewallBackend:
    """The host's netfilter backend, detected once."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = detect_backend()
    return _BACKEND


def _resolve_vmachine_ip(vmachine_id: str, source_ip: Optional[str] = None) -> str:
    if source_ip and str(source_ip).strip():
        return str(source_ip).strip()

    state = load_runtime_state(vmachine_id)
    if state:
        ip = state.get("ip")
        if isinstance(ip, str) and ip.strip():
            return ip.strip()

    ip = sc.get_internal_ip(id=vmachine_id)
    if ip and str(ip).strip():
        return str(ip).strip()

    raise RuntimeError(f"Unable to resolve VM IP for firewall rules: {vmachine_id}")


def _apply(rules: List[Rule], *, context: str) -> bool:
    active = backend()
    for rule in rules:
        try:
            added = active.ensure(rule)
        except FirewallError as e:
            logger(f"[CH][FW] {context}: {e}")
            return False
        if not added:
            logger(f"[CH][FW] {context}: already in place ({rule.comment})")
    return True


def block_all(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        rules = policy.block_all_rules(vmachine_id=vmachine_id, vm_ip=vm_ip)
    except Exception as e:
        logger(f"[CH][FW] Failed to block all traffic for {vmachine_id}: {e}")
        return False

    if not _apply(rules, context=f"block_all for {vmachine_id} ({vm_ip})"):
        return False
    logger(f"[CH][FW] Blocked all outgoing traffic for {vmachine_id} ({vm_ip})")
    return True


def allow_connection(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        rule = policy.allow_connection_rule(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            ip=ip,
            port=port,
            protocol=protocol.value,
        )
    except Exception as e:
        logger(f"[CH][FW] Failed to allow connection for {vmachine_id}: {e}")
        return False

    target = f"{ip}:{port}" if port is not None else str(ip)
    if not _apply([rule], context=f"allow {vm_ip} -> {target}/{protocol.value}"):
        return False
    logger(
        f"[CH][FW] Allowed {protocol.value} from {vmachine_id} ({vm_ip}) to {target}"
    )
    return True


def allow_host_connection(
    vmachine_id: str,
    host_ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    """Allow a guest to reach a service on this host (input hook, not forward).

    See ``policy.allow_host_connection_rule``: traffic to the host's own address
    never traverses the forward chain, so the ordinary ``allow_connection`` writes
    a rule that can never match for these destinations.
    """
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        rule = policy.allow_host_connection_rule(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            host_ip=host_ip,
            port=port,
            protocol=protocol.value,
        )
    except Exception as e:
        logger(f"[CH][FW] Failed to allow host connection for {vmachine_id}: {e}")
        return False

    target = f"{host_ip}:{port}" if port is not None else str(host_ip)
    if not _apply([rule], context=f"allow_host {vm_ip} -> {target}/{protocol.value}"):
        return False
    logger(
        f"[CH][FW] Allowed {protocol.value} from {vmachine_id} ({vm_ip}) to this host at {target}"
    )
    return True


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
    source_ip: Optional[str] = None,
) -> bool:
    try:
        slot_protocols = {
            slot.port: resolve_slot_transport_protocols(
                slot,
                logger_fn=logger,
                context=f"[CH][FW][{vmachine_id}]",
            )
            for slot in instance.api.slot
        }

        results: List[bool] = []
        for slot in instance.uri_slot:
            internal_port = slot.internal_port
            if internal_port not in slot_protocols:
                logger(
                    f"[CH][FW] Internal slot {internal_port} not present in instance.api.slot. Skipping."
                )
                continue
            protocol = slot_protocols[internal_port]
            if not protocol:
                logger(
                    f"[CH][FW] Internal slot {internal_port} has no host-supported transports. Skipping."
                )
                continue

            for uri in slot.uri:
                result = allow_connection(
                    vmachine_id=vmachine_id,
                    ip=uri.ip,
                    port=uri.port,
                    protocol=protocol,
                    source_ip=source_ip,
                )
                if not result:
                    logger(
                        f"[CH][FW] Failed allow_connection_to_instance for {vmachine_id} "
                        f"towards {uri.ip}:{uri.port}/{protocol.value}"
                    )
                results.append(result)

        if not any(results):
            raise RuntimeError("No allow rule could be applied for any instance slot.")
        return True
    except Exception as e:
        logger(f"[CH][FW] Failed to allow connection to instance for {vmachine_id}: {e}")
        return False


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    """Remove one allow rule, matched by the comment that created it."""
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        rule = policy.allow_connection_rule(
            vmachine_id=vmachine_id,
            vm_ip=vm_ip,
            ip=ip,
            port=port,
            protocol=protocol.value,
        )
        removed = backend().delete_by_comment(Chain.FORWARD, rule.comment)
    except Exception as e:
        logger(f"[CH][FW] Failed to remove rule for {vmachine_id}: {e}")
        return False

    target = f"{ip}:{port}" if port is not None else str(ip)
    if removed:
        logger(f"[CH][FW] Removed {protocol.value} rule for {vmachine_id} to {target}")
        return True
    logger(f"[CH][FW] No {protocol.value} rule for {vmachine_id} to {target} to remove")
    return False


def allow_all_egress(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    """Allow ALL outbound traffic from the VM (used for network tag '*').

    Goes in at the head so it short-circuits the blanket drop ``block_all``
    installed. Return traffic is covered by the global RELATED,ESTABLISHED accept.
    """
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        rule = policy.allow_all_egress_rule(vmachine_id=vmachine_id, vm_ip=vm_ip)
    except Exception as e:
        logger(f"[CH][FW] Failed to allow all egress for {vmachine_id}: {e}")
        return False

    if not _apply([rule], context=f"allow_all_egress for {vmachine_id} ({vm_ip})"):
        return False
    logger(f"[CH][FW] Allowed ALL egress for {vmachine_id} ({vm_ip}) [network tag '*']")
    return True


def remove_vm_rules(vmachine_id: str) -> int:
    """Delete every rule nodo wrote for this VM, across every chain.

    One prefix instead of replaying the arguments that created each rule, so a
    teardown cannot leave an orphan hole behind because the recorded removal
    command no longer matches.
    """
    try:
        prefix = policy.vm_comment_prefix(vmachine_id)
        removed = backend().delete_by_comment_prefix(prefix)
    except Exception as e:
        logger(f"[CH][FW] Failed to remove the rules of {vmachine_id}: {e}")
        return 0

    if removed:
        logger(f"[CH][FW] Removed {removed} firewall rule(s) for {vmachine_id}")
    return removed
