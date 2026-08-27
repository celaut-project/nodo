from enum import Enum
from typing import Callable, List, Optional

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.firewall import policy
from src.utils.firewall.errors import FirewallError
from src.utils.logger import LOGGER as logger

sc = SQLConnection()
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


def _normalize_virtualizer(name: Optional[str]) -> str:
    if not isinstance(name, str):
        raise ValueError(f"Invalid virtualizer value: {name!r}")
    v = name.strip().lower()
    if not v:
        raise ValueError("Virtualizer value is empty.")
    if v in {"ch", "cloud_hypervisor", "cloud-hypervisor"}:
        return "ch"
    if v == "qemu":
        return "qemu"
    raise ValueError(f"Unknown virtualizer '{name}'. Supported: ch, qemu.")


def _resolve_virtualizer(vmachine_id: str) -> str:
    try:
        virtualizer = sc.get_internal_virtualizer(id=vmachine_id)
        if isinstance(virtualizer, str) and virtualizer.strip():
            return _normalize_virtualizer(virtualizer)
    except Exception:
        pass
    default_virtualizer = env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "ch")
    return _normalize_virtualizer(default_virtualizer)


def ensure_forward_related_established_rule() -> bool:
    """Ensure the blanket accept for return traffic is the first FORWARD rule.

    It has to be first: everything below it is the per-VM policy, and return
    traffic for an already-allowed connection must never be evaluated against
    that. Duplicates left by an earlier start are collapsed rather than tolerated.
    """
    from src.virtualizers.ch.firewall import backend

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


# There is no ``allow_connection`` here on purpose. A bare "let this guest reach
# this ip:port" had two callers, both of them host destinations, and both now go
# through ``allow_host_connection`` below. What remains of the routed case is
# ``allow_connection_to_instance``, which is the shape callers actually hold (an
# Instance with its slots and protocols); it reaches the forward-hook allow inside
# ``ch.firewall`` directly. A dispatcher wrapper nobody dispatches through is worse
# than none: it is dead code that its own tests make look alive.
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
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import (
            allow_host_connection as ch_allow_host_connection,
        )

        return ch_allow_host_connection(
            vmachine_id=vmachine_id,
            host_ip=host_ip,
            port=port,
            protocol=protocol,
            source_ip=source_ip,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
    source_ip: Optional[str] = None,
) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import (
            allow_connection_to_instance as ch_allow_connection_to_instance,
        )

        return ch_allow_connection_to_instance(
            vmachine_id=vmachine_id,
            instance=instance,
            source_ip=source_ip,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def block_all(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    if not ensure_forward_related_established_rule():
        return False

    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import block_all as ch_block_all

        return ch_block_all(vmachine_id=vmachine_id, source_ip=source_ip)

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def allow_all_egress(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import allow_all_egress as ch_allow_all_egress

        return ch_allow_all_egress(vmachine_id=vmachine_id, source_ip=source_ip)

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
) -> bool:
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import remove_rule as ch_remove_rule

        return ch_remove_rule(
            vmachine_id=vmachine_id,
            ip=ip,
            port=port,
            protocol=protocol,
        )

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")


def remove_vm_rules(vmachine_id: str) -> int:
    """Delete every rule nodo wrote for one VM, whatever the virtualizer."""
    virtualizer = _resolve_virtualizer(vmachine_id)
    if virtualizer in ("ch", "qemu"):
        from src.virtualizers.ch.firewall import remove_vm_rules as ch_remove_vm_rules

        return ch_remove_vm_rules(vmachine_id=vmachine_id)

    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")
