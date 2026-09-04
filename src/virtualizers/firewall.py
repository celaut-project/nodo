from enum import Enum
from typing import Callable, List, Optional

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.firewall import policy
from src.utils.firewall.errors import FirewallError
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()

FORWARD_RELATED_ESTABLISHED_COMMENT = policy.FORWARD_RELATED_ESTABLISHED_COMMENT


class TransportProtocol(Enum):
    """Supported network protocols."""

    TCP = "tcp"
    UDP = "udp"


SUPPORTED_TRANSPORT_TAGS = {
    TransportProtocol.TCP.value: TransportProtocol.TCP,
    TransportProtocol.UDP.value: TransportProtocol.UDP,
}


def serialize_transport_protocol(protocol: Optional[TransportProtocol]) -> bytes:
    if not protocol:
        return b""
    return str(protocol.value).strip().lower().encode("utf-8")


def resolve_slot_transport_protocols(
    slot: celaut.Service.Api.Slot,
    *,
    logger_fn: Callable[[str], None] = logger,
    context: str = "[FW]",
) -> Optional[TransportProtocol]:
    if slot is None:
        raise ValueError(f"{context} Slot is None.")

    if not slot.HasField("transport"):
        raise ValueError(
            f"{context} Slot port={slot.port} is missing required transport definition."
        )

    normalized_tags = [
        str(tag).strip().lower()
        for tag in slot.transport.tags
        if str(tag).strip()
    ]
    if not normalized_tags:
        raise ValueError(
            f"{context} Slot port={slot.port} has empty transport tags. At least one tag is required."
        )

    resolved: List[TransportProtocol] = []
    for tag in normalized_tags:
        protocol = SUPPORTED_TRANSPORT_TAGS.get(tag)
        if not protocol:
            logger_fn(
                f"{context} Slot port={slot.port} transport tag '{tag}' is unsupported by host. "
                "Supported tags: tcp, udp. Slot transport tag ignored."
            )
            continue
        if protocol not in resolved:
            resolved.append(protocol)

    if len(resolved) > 1:
        raise ValueError(
            f"{context} Slot port={slot.port} declares multiple transport families ({[p.value for p in resolved]}). "
            "Each slot must resolve to a single transport."
        )

    if not resolved:
        logger_fn(
            f"{context} Slot port={slot.port} has no host-supported transport tags. Slot will be ignored."
        )
        return None

    return resolved[0]


# There is no per-instance dispatch in this module, on purpose.
#
# It used to resolve the instance's backend from the database and then branch on
# it -- six times, and all six branches read `if virtualizer in ("ch", "qemu")`
# followed by the same call. That is not dispatch, it is a database lookup whose
# answer is discarded, and its tests made the dead half look alive.
#
# The reason no branch was reachable is the real answer: a per-VM rule is written
# against a tap on a bridge, by comment prefix, with iptables or nft. That is not
# a property of *a backend* but of the whole microVM family, whose members share
# the one bridge those rules sit on -- so both of this node's backends resolve to
# the same implementation, and a third microVM hypervisor would too.
#
# A backend outside that family does not need a narrower branch here; it has no
# tap and no bridge, so it has nothing for these functions to write. When one
# exists, what changes is this module's contract (does the node even ask for a
# firewall rule for such a guest?), not a branch inside it -- and the registry
# already knows each backend's family for whoever has to answer that.


def ensure_forward_related_established_rule() -> bool:
    """Ensure the blanket accept for return traffic is the first FORWARD rule.

    It has to be first: everything below it is the per-VM policy, and return
    traffic for an already-allowed connection must never be evaluated against
    that. Duplicates left by an earlier start are collapsed rather than tolerated.
    """
    from src.virtualizers.microvm.firewall import backend

    try:
        added = backend().ensure_first(policy.forward_related_established_rule())
    except FirewallError as e:
        logger(f"[FW] Failed ensuring global FORWARD RELATED,ESTABLISHED rule: {e}")
        return False
    except Exception as e:
        logger(f"[FW] Unexpected error ensuring global FORWARD RELATED,ESTABLISHED rule: {e}")
        return False

    if added:
        logger("[FW] Ensured global FORWARD RELATED,ESTABLISHED rule in position 1.")
    return True


def _compat_mode():
    """``virtualizers.ch.FORWARD_COMPAT``, defaulting to auto on an unreadable value.

    A typo here must not stop a node from booting: the key decides whether nodo
    compensates for someone else's forward policy, and refusing to start over it
    would be a worse outcome than either answer it can hold.
    """
    from src.utils.firewall.compat import CompatMode

    raw = env_manager.get("virtualizers.ch.FORWARD_COMPAT", "auto")
    try:
        return CompatMode.parse(raw)
    except ValueError as e:
        logger(f"[FW] {e} Falling back to auto.")
        return CompatMode.AUTO


def _compat_bridge(bridge: Optional[str] = None) -> str:
    name = (bridge or env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch") or "")
    return str(name).strip()


def ensure_forward_compat(bridge: Optional[str] = None):
    """Put nodo's compatibility chain in place, if this host turns out to need one.

    Deliberately not folded into ``ensure_forward_related_established_rule``: that
    writes a rule nodo owns outright, this one writes into a table it does not, and
    a failure of the second must never take down the first. Never raises -- see
    ``compat.ensure_compat`` for why this is not fatal.
    """
    from src.utils.firewall.compat import ensure_compat

    return ensure_compat(_compat_bridge(bridge), _compat_mode(), log=logger)


def remove_forward_compat(bridge: Optional[str] = None):
    """Take nodo's compatibility chain back out of the host's FORWARD chain."""
    from src.utils.firewall.compat import remove_compat

    return remove_compat(_compat_bridge(bridge), log=logger)


def forward_compat_state(bridge: Optional[str] = None):
    """What is in the compatibility table right now. Writes nothing."""
    from src.utils.firewall.compat import compat_state

    return compat_state(_compat_bridge(bridge), _compat_mode())


# There is no ``allow_connection`` here on purpose. A bare "let this guest reach
# this ip:port" had two callers, both of them host destinations, and both now go
# through ``allow_host_connection`` below. What remains of the routed case is
# ``allow_connection_to_instance``, which is the shape callers actually hold (an
# Instance with its slots and protocols); it reaches the forward-hook allow inside
# ``microvm.firewall`` directly. A dispatcher wrapper nobody dispatches through is
# worse than none: it is dead code that its own tests make look alive.
def allow_host_connection(
    vmachine_id: str,
    host_ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    """A guest reaching a service on this host: an input-hook rule, not a forward one.

    No ``ensure_forward_related_established_rule`` here, unlike the forward-side
    allows: that accept exists so return traffic for an allowed *forwarded*
    connection is not re-evaluated against the blanket drop. This rule carries no
    conntrack state of its own, so it already matches every packet of the flow it
    permits, and the host's reply leaves through output.
    """
    from src.virtualizers.microvm.firewall import allow_host_connection as fw_allow_host_connection

    return fw_allow_host_connection(
        vmachine_id=vmachine_id,
        host_ip=host_ip,
        port=port,
        protocol=protocol,
        source_ip=source_ip,
    )


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
    source_ip: Optional[str] = None,
) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    from src.virtualizers.microvm.firewall import (
        allow_connection_to_instance as fw_allow_connection_to_instance,
    )

    return fw_allow_connection_to_instance(
        vmachine_id=vmachine_id,
        instance=instance,
        source_ip=source_ip,
    )


def block_all(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    from src.virtualizers.microvm.firewall import block_all as fw_block_all

    return fw_block_all(vmachine_id=vmachine_id, source_ip=source_ip)


def allow_all_egress(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    from src.virtualizers.microvm.firewall import allow_all_egress as fw_allow_all_egress

    return fw_allow_all_egress(vmachine_id=vmachine_id, source_ip=source_ip)


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
    from src.virtualizers.microvm.firewall import remove_rule as fw_remove_rule

    return fw_remove_rule(
        vmachine_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol,
    )


def remove_vm_rules(vmachine_id: str) -> int:
    """Delete every rule nodo wrote for one VM, whatever the virtualizer."""
    from src.virtualizers.microvm.firewall import remove_vm_rules as fw_remove_vm_rules

    return fw_remove_vm_rules(vmachine_id=vmachine_id)
