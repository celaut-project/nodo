"""Clean up rules earlier nodo versions left in the iptables compatibility tables.

Before this package, everything went through the ``iptables`` binary. On a host
where that binary is the ``iptables-nft`` shim, those rules live in the ``ip
filter``/``ip nat`` compatibility tables -- a different place from nodo's own
``inet nodo``/``ip nodo``. Left alone after an upgrade they are invisible
duplicates at best, and at worst a stale DNAT still pointing a published port at
a VM that no longer exists.

So when nftables is the active backend, sweep them once at start-up. When
iptables *is* the active backend they are the live rules and must be left alone.

POSTROUTING is deliberately excluded: the only rule nodo puts there is the guest
subnet masquerade, and a duplicate is harmless (a connection is NAT'd once) while
a gap in it would cut outbound connectivity for every running instance.
"""

from typing import Callable, List, Sequence

from src.utils.firewall.backends import FirewallBackend, IptablesBackend, detect_backend
from src.utils.firewall.errors import FirewallError
from src.utils.firewall.rules import Chain

LEGACY_COMMENT_ROOT = "nodo;"

SWEPT_CHAINS: Sequence[Chain] = (Chain.INPUT, Chain.FORWARD, Chain.PREROUTING)


def sweep_compat_tables(
    active: FirewallBackend = None,
    *,
    chains: Sequence[Chain] = SWEPT_CHAINS,
    log: Callable[[str], None] = lambda message: None,
) -> int:
    """Remove nodo-commented rules from the iptables compatibility tables.

    No-op when iptables is the active backend. Best-effort throughout: an
    orphaned rule we could not delete is worth a log line, not a failed start.
    """
    backend = active or detect_backend()
    if isinstance(backend, IptablesBackend):
        return 0

    compat = IptablesBackend(run=backend._run)
    removed = 0
    for chain in chains:
        try:
            stale = compat.list_rules(chain, LEGACY_COMMENT_ROOT)
        except FirewallError as e:
            log(f"[FW] Could not read the compatibility {chain.value} chain: {e}")
            continue
        for rule in reversed(stale):
            try:
                compat.delete(rule)
                removed += 1
                log(f"[FW] Removed a pre-nftables rule from {chain.value}: {rule.comment}")
            except FirewallError as e:
                log(f"[FW] Could not remove a pre-nftables rule ({rule.comment}): {e}")
    return removed
