"""A netfilter rule as data, so both backends can render the same intent.

Only the shapes nodo actually uses are modelled. That is deliberate: a general
rule builder would be far more code and far more ways to be wrong, and every rule
this node writes fits in here.

Every rule carries a **unique** comment, and deletion is by comment. That is what
makes tearing a VM's rules down correct on both backends: ``nft`` deletes by
handle and ``iptables`` by reconstructing the rule from its own ``-S`` output, so
neither has to re-derive the exact arguments a previous version of nodo used.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from src.utils.firewall.errors import RuleError

# nft caps comments at 128 bytes; iptables allows 256. Stay under the smaller one.
MAX_COMMENT_LENGTH = 127


class Chain(Enum):
    """The hooks nodo writes to."""

    INPUT = "input"
    FORWARD = "forward"
    PREROUTING = "prerouting"
    POSTROUTING = "postrouting"

    @property
    def is_nat(self) -> bool:
        return self in (Chain.PREROUTING, Chain.POSTROUTING)


class Verdict(Enum):
    ACCEPT = "accept"
    DROP = "drop"
    MASQUERADE = "masquerade"
    DNAT = "dnat"


def _validate_port(value: Optional[int], label: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuleError(f"{label} must be an int, got {value!r}.")
    if not 1 <= value <= 65535:
        raise RuleError(f"{label} {value} is outside 1-65535.")
    return value


@dataclass(frozen=True)
class Rule:
    """One netfilter rule, backend-agnostic.

    ``at_head`` mirrors ``iptables -I``: nodo's egress policy depends on ordering,
    because the per-VM allow rules must sit above the blanket NEW drop that
    ``block_all`` installs. Rules added later with ``at_head`` therefore take
    precedence, exactly as they did with ``-I``.
    """

    chain: Chain
    comment: str
    verdict: Verdict = Verdict.ACCEPT
    source: Optional[str] = None
    destination: Optional[str] = None
    destination_is_negated: bool = False
    protocol: Optional[str] = None
    dport: Optional[int] = None
    sport: Optional[int] = None
    ct_states: Tuple[str, ...] = field(default_factory=tuple)
    dnat_to: Optional[str] = None
    at_head: bool = False

    def __post_init__(self):
        if not self.comment or not self.comment.strip():
            raise RuleError("Every nodo rule must carry a comment; deletion is by comment.")
        if len(self.comment) > MAX_COMMENT_LENGTH:
            raise RuleError(
                f"Comment is {len(self.comment)} bytes, over the {MAX_COMMENT_LENGTH} "
                f"nft allows: {self.comment}"
            )
        if self.protocol is not None and self.protocol not in ("tcp", "udp"):
            raise RuleError(f"Unsupported protocol {self.protocol!r}. Supported: tcp, udp.")
        if (self.dport is not None or self.sport is not None) and not self.protocol:
            raise RuleError("A port match needs a protocol.")
        _validate_port(self.dport, "dport")
        _validate_port(self.sport, "sport")
        if self.verdict is Verdict.DNAT and not self.dnat_to:
            raise RuleError("A DNAT rule needs dnat_to.")
        if self.verdict is not Verdict.DNAT and self.dnat_to:
            raise RuleError("dnat_to only applies to a DNAT rule.")
        if self.verdict is Verdict.MASQUERADE and self.chain is not Chain.POSTROUTING:
            raise RuleError("MASQUERADE only belongs in postrouting.")
        if self.verdict is Verdict.DNAT and self.chain is not Chain.PREROUTING:
            raise RuleError("DNAT only belongs in prerouting.")
        if self.destination_is_negated and not self.destination:
            raise RuleError("A negated destination needs a destination.")


def truncate_comment(comment: str) -> str:
    """Keep a comment inside nft's limit without losing its identifying tail.

    The tail is what distinguishes one rule from another (address, port,
    protocol), so an over-long comment loses characters from the middle of the
    VM id rather than from either end.
    """
    if len(comment) <= MAX_COMMENT_LENGTH:
        return comment
    keep = MAX_COMMENT_LENGTH - 3
    head = keep // 2
    tail = keep - head
    return f"{comment[:head]}...{comment[-tail:]}"
