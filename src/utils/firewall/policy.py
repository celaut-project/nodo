"""What rules a guest's network policy consists of, as pure functions.

Separated from the virtualizer adapters on purpose. Deciding *which* rules to
write is the part that enforces isolation -- a mistake here silently opens a
guest's egress -- while resolving a VM's address needs the database and the
config. Keeping the decision pure means it can be tested exhaustively without a
node, which is the only way this code gets real coverage.

Rule ordering matters and is preserved from the iptables implementation this
replaces: ``block_all`` installs a blanket drop of NEW packets, and each
subsequent allow is inserted *above* it. So an allow added later wins, exactly as
``iptables -I`` behaved.

Every rule carries a comment that is unique for what it does and prefixed with
the VM it belongs to, so a whole instance's footprint can be torn down with one
prefix instead of replaying the arguments that created it.
"""

import ipaddress
from typing import List, Optional, Tuple

from src.utils.firewall.errors import RuleError
from src.utils.firewall.rules import Chain, Rule, Verdict, truncate_comment

PROTOCOLS: Tuple[str, ...] = ("tcp", "udp")

FORWARD_RELATED_ESTABLISHED_COMMENT = "nodo;forward;related_established"
MASQUERADE_COMMENT_PREFIX = "nodo;masquerade;subnet="
VM_COMMENT_ROOT = "nodo;vm="


def vm_comment_prefix(vmachine_id: str) -> str:
    """Everything nodo writes for one VM starts with this."""
    identifier = str(vmachine_id).strip()
    if not identifier:
        raise RuleError("A VM rule needs a vmachine_id.")
    return f"{VM_COMMENT_ROOT}{identifier};"


def _comment(vmachine_id: str, suffix: str) -> str:
    return truncate_comment(f"{vm_comment_prefix(vmachine_id)}{suffix}")


def validate_address(value: str, label: str) -> str:
    """A bare address or CIDR, rejected outright if it is neither.

    Replaces the argument-shape regex the iptables path used: these values come
    from config and the database, and a malformed one must never be handed to a
    firewall command.
    """
    text = str(value or "").strip()
    if not text:
        raise RuleError(f"{label} is empty.")
    try:
        if "/" in text:
            ipaddress.ip_network(text, strict=False)
        else:
            ipaddress.ip_address(text)
    except ValueError as e:
        raise RuleError(f"{label} {text!r} is not an address or CIDR: {e}") from e
    return text


def _validate_protocol(protocol: str) -> str:
    text = str(protocol or "").strip().lower()
    if text not in PROTOCOLS:
        raise RuleError(f"Unsupported protocol {protocol!r}. Supported: {', '.join(PROTOCOLS)}.")
    return text


def forward_related_established_rule() -> Rule:
    """The blanket accept for return traffic, which must sit first in the chain."""
    return Rule(
        chain=Chain.FORWARD,
        comment=FORWARD_RELATED_ESTABLISHED_COMMENT,
        verdict=Verdict.ACCEPT,
        ct_states=("RELATED", "ESTABLISHED"),
        at_head=True,
    )


def block_all_rules(vmachine_id: str, vm_ip: str) -> List[Rule]:
    """Drop every new connection a guest starts. The default it opts out of."""
    source = validate_address(vm_ip, "VM address")
    return [
        Rule(
            chain=Chain.FORWARD,
            comment=_comment(vmachine_id, f"block_all;{protocol}"),
            verdict=Verdict.DROP,
            source=source,
            protocol=protocol,
            ct_states=("NEW",),
            at_head=True,
        )
        for protocol in PROTOCOLS
    ]


def allow_connection_rule(
    vmachine_id: str,
    vm_ip: str,
    ip: str,
    port: Optional[int] = None,
    protocol: str = "tcp",
) -> Rule:
    """Let one guest reach one destination, above the blanket drop."""
    source = validate_address(vm_ip, "VM address")
    destination = validate_address(ip, "Destination address")
    resolved = _validate_protocol(protocol)
    target = f"{destination}:{port}" if port is not None else destination
    return Rule(
        chain=Chain.FORWARD,
        comment=_comment(vmachine_id, f"allow;{target}/{resolved}"),
        verdict=Verdict.ACCEPT,
        source=source,
        destination=destination,
        protocol=resolved,
        dport=port,
        at_head=True,
    )


def allow_host_connection_rule(
    vmachine_id: str,
    vm_ip: str,
    host_ip: str,
    port: Optional[int] = None,
    protocol: str = "tcp",
) -> Rule:
    """Let one guest reach a service on the host itself.

    Separate from ``allow_connection_rule`` because of the chain, and the chain is
    the whole point. A packet addressed to one of the host's own addresses -- the
    guest bridge's gateway IP is one -- is delivered locally, so it is evaluated on
    the *input* hook and never reaches forward. The same allow written in FORWARD,
    which is what nodo wrote for the node's own gateway port and for the guest
    resolver, cannot match a single packet: it sits in the ruleset (and is
    announced in the log) as though it granted an access it plays no part in.

    There is deliberately no input-side counterpart to ``block_all``: the node's
    gateway has to stay reachable for every guest that runs on it, and the accept
    that opens that port (``firewall.gateway.ensure_gateway_port_open``) is
    source-agnostic on purpose, because peers reach the same port from off-host.
    So this rule states a guest's intended access to the host rather than gating
    it -- but stated on the hook where it can be true.
    """
    source = validate_address(vm_ip, "VM address")
    destination = validate_address(host_ip, "Host address")
    resolved = _validate_protocol(protocol)
    target = f"{destination}:{port}" if port is not None else destination
    return Rule(
        chain=Chain.INPUT,
        comment=_comment(vmachine_id, f"allow_host;{target}/{resolved}"),
        verdict=Verdict.ACCEPT,
        source=source,
        destination=destination,
        protocol=resolved,
        dport=port,
        at_head=True,
    )


def allow_all_egress_rule(vmachine_id: str, vm_ip: str) -> Rule:
    """Unrestricted egress, for a service whose network tag is '*'."""
    return Rule(
        chain=Chain.FORWARD,
        comment=_comment(vmachine_id, "allow_all_egress"),
        verdict=Verdict.ACCEPT,
        source=validate_address(vm_ip, "VM address"),
        at_head=True,
    )


def masquerade_rule(subnet: str) -> Rule:
    """Source-NAT the guest subnet on its way off this host.

    Global rather than per-VM: removing it while another instance is running
    would cut that instance's connectivity, so it is never part of a VM teardown.
    """
    network = validate_address(subnet, "Guest subnet")
    if "/" not in network:
        raise RuleError(f"Guest subnet {network!r} must be a CIDR.")
    return Rule(
        chain=Chain.POSTROUTING,
        comment=f"{MASQUERADE_COMMENT_PREFIX}{network}",
        verdict=Verdict.MASQUERADE,
        source=network,
        destination=network,
        destination_is_negated=True,
    )


def port_forward_rules(
    vmachine_id: str,
    vm_ip: str,
    protocol: str,
    external_port: int,
    internal_port: int,
) -> List[Rule]:
    """Publish a guest port on the host: the DNAT plus the two it needs to work.

    PREROUTING is where the translation happens; the FORWARD pair is what lets
    the translated packet and its replies through the guest policy. OUTPUT is
    deliberately not involved -- it only sees traffic the host itself originates.
    """
    destination = validate_address(vm_ip, "VM address")
    resolved = _validate_protocol(protocol)
    external = int(external_port)
    internal = int(internal_port)

    return [
        Rule(
            chain=Chain.PREROUTING,
            comment=_comment(vmachine_id, f"dnat;{resolved};{external}"),
            verdict=Verdict.DNAT,
            protocol=resolved,
            dport=external,
            dnat_to=f"{destination}:{internal}",
        ),
        Rule(
            chain=Chain.FORWARD,
            comment=_comment(vmachine_id, f"dnat_in;{resolved};{internal}"),
            verdict=Verdict.ACCEPT,
            destination=destination,
            protocol=resolved,
            dport=internal,
            ct_states=("NEW", "ESTABLISHED", "RELATED"),
        ),
        Rule(
            chain=Chain.FORWARD,
            comment=_comment(vmachine_id, f"dnat_out;{resolved};{internal}"),
            verdict=Verdict.ACCEPT,
            source=destination,
            protocol=resolved,
            sport=internal,
            ct_states=("ESTABLISHED", "RELATED"),
        ),
    ]
