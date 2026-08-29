"""The one hole nodo punches in a table it does not own -- and how to take it back.

Why there is one at all
-----------------------
nodo filters in ``inet nodo`` at priority -5. ``accept`` there ends that chain and
nothing more: the packet still traverses every other base chain on the hook, and a
``drop`` in any of them is terminal. Guest-to-guest traffic is routed through the
host on purpose (``_ensure_guest_l2_isolation``) so nodo's policy can see it, which
also means a foreign forward chain gets a say. Docker sets ``ip filter FORWARD``'s
policy to DROP for every host it is installed on; ufw defaults
``DEFAULT_FORWARD_POLICY`` to DROP. On such a host a parent cannot reach the child
it just launched, and the failure arrives disguised as a child that died.

What this does, and what it refuses to do
-----------------------------------------
It creates ``NODO_FWD`` in the ``ip filter`` compatibility table, jumps to it from
FORWARD, and puts four narrow accepts in it -- one per guest path nodo actually
needs (see ``policy.compat_rules``). A named chain rather than loose rules,
because a chain has an owner: ``iptables -S NODO_FWD`` is a complete answer to
"what did nodo add", and ``remove_compat`` is one operation rather than a hunt.

It does not weaken isolation. nodo's own filter runs earlier, in another table,
and its drop is terminal -- an accept here is never consulted for a guest nodo has
decided to isolate.

It is off unless it is needed. ``auto`` asks the live ruleset whether anything on
the forward hook can actually drop this traffic and does nothing when the answer is
no, so a clean host keeps a clean FORWARD chain.

Coverage, stated rather than implied
------------------------------------
This reaches exactly one table: ``ip filter``, the one ``iptables`` writes to,
which is where Docker, ufw and most CNI plugins put their forward policy. It does
**not** reach a firewall that owns a native nftables table of its own -- firewalld
is the one that matters, with ``inet firewalld``'s ``filter_FORWARD`` at priority
10 rejecting forwarded traffic by zone, and it is the default on Fedora and RHEL.
Nothing nodo can write in ``ip filter`` changes that verdict. There the operator
has to allow forwarding for the guest bridge in firewalld itself, and ``nodo
doctor`` says so by name rather than leaving them to guess.
"""

import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple

from src.utils.firewall import policy
from src.utils.firewall.backends import (
    COMPAT_CHAIN,
    FirewallBackend,
    IptablesBackend,
    Runner,
    detect_backend,
)
from src.utils.firewall.errors import FirewallError
from src.utils.firewall.rules import Chain

Log = Callable[[str], None]


def _silent(_message: str) -> None:
    pass


class CompatMode(Enum):
    """Whether nodo may write into the compatibility table at all.

    ``auto`` is the default and the only one that looks before it writes: it
    applies the rules when the live ruleset shows something on the forward hook
    that can drop guest traffic, and leaves a clean host alone. ``on`` is for a
    host whose ruleset cannot be read but whose operator knows the answer. ``off``
    is an operator saying nodo must not touch a table it does not own, which is a
    legitimate position -- the node still runs, and doctor still reports the
    consequence.
    """

    AUTO = "auto"
    ON = "on"
    OFF = "off"

    @classmethod
    def parse(cls, value: Optional[str]) -> "CompatMode":
        text = str(value or "").strip().lower()
        if not text:
            return cls.AUTO
        for mode in cls:
            if mode.value == text:
                return mode
        raise ValueError(
            f"Unknown FORWARD_COMPAT value {value!r}. Use one of: "
            + ", ".join(mode.value for mode in cls)
        )


@dataclass(frozen=True)
class CompatState:
    """What is actually in the compatibility table right now, plus what it means."""

    mode: CompatMode
    available: bool = True
    chain_present: bool = False
    jump_present: bool = False
    rules_present: Tuple[str, ...] = ()
    rules_expected: Tuple[str, ...] = ()
    changed: bool = False
    needed: Optional[bool] = None
    detail: str = ""
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        """Every piece in place: the chain, the jump into it, and all four rules."""
        return (
            self.chain_present
            and self.jump_present
            and set(self.rules_expected).issubset(set(self.rules_present))
        )

    @property
    def partial(self) -> bool:
        """Something of nodo's is there, but not all of it. Worse than neither."""
        return not self.complete and (
            self.chain_present or self.jump_present or bool(self.rules_present)
        )

    def describe(self) -> List[str]:
        lines = [self.detail] if self.detail else []
        if self.error:
            lines.append(f"The last attempt failed: {self.error}")
        return lines


def _iptables_backend(run: Optional[Runner] = None) -> IptablesBackend:
    return IptablesBackend(run=run) if run else IptablesBackend()


def _forward_hook_is_clear(run: Optional[Runner] = None) -> Tuple[Optional[bool], str]:
    """Does anything outside nodo's tables drop on the forward hook?

    Three answers, and the third one matters: a ruleset nobody could read is not a
    clear hook. ``auto`` applies the rules in that case rather than assuming the
    best, because the cost of an unnecessary accept in nodo's own chain is four
    visible lines an operator can delete, and the cost of the opposite mistake is
    a node whose services cannot reach their own dependencies.
    """
    try:
        backend: FirewallBackend = detect_backend(run=run) if run else detect_backend()
        scan = backend.foreign_forward_rejectors()
    except Exception as e:
        return None, f"the forward hook could not be read ({e})"
    if not scan.readable:
        return None, f"the forward hook could not be read ({scan.reason})"
    if not scan.rejectors:
        return True, "nothing outside nodo's ruleset drops on the forward hook"
    names = ", ".join(f"{r.table}/{r.chain}" for r in scan.rejectors)
    return False, f"the forward hook is not clear: {names}"


def compat_state(
    bridge: str,
    mode: CompatMode = CompatMode.AUTO,
    *,
    run: Optional[Runner] = None,
) -> CompatState:
    """Read what is in the compatibility table. Writes nothing."""
    expected = tuple(rule.comment for rule in policy.compat_rules(bridge))

    if not shutil.which("iptables"):
        return CompatState(
            mode=mode,
            available=False,
            rules_expected=expected,
            detail="There is no iptables on this host, so there is no compatibility "
                   "table to write to and nothing to compensate for.",
        )

    backend = _iptables_backend(run)
    try:
        jumps = [
            applied for applied in backend.list_rules(Chain.FORWARD, policy.COMPAT_JUMP_COMMENT)
            if applied.comment == policy.COMPAT_JUMP_COMMENT
        ]
    except FirewallError as e:
        return CompatState(
            mode=mode, rules_expected=expected, error=str(e),
            detail="The FORWARD chain could not be read.",
        )

    # Asked before listing: an absent chain is the ordinary state of a host that
    # never needed one, and must not read as a chain that could not be listed.
    present: Tuple[str, ...] = ()
    try:
        chain_present = backend.own_chain_exists(Chain.FORWARD_COMPAT)
        if chain_present:
            present = tuple(
                applied.comment
                for applied in backend.list_rules(
                    Chain.FORWARD_COMPAT, policy.COMPAT_COMMENT_PREFIX
                )
            )
    except FirewallError as e:
        return CompatState(
            mode=mode, rules_expected=expected, jump_present=bool(jumps), error=str(e),
            detail=f"The {COMPAT_CHAIN} chain could not be read.",
        )

    state = CompatState(
        mode=mode,
        chain_present=chain_present,
        jump_present=bool(jumps),
        rules_present=present,
        rules_expected=expected,
    )
    return _with_detail(state, bridge)


def _with_detail(state: CompatState, bridge: str) -> CompatState:
    if state.complete:
        detail = (
            f"{COMPAT_CHAIN} is in place: FORWARD jumps to it and it holds nodo's "
            f"{len(state.rules_expected)} accepts for {bridge}."
        )
    elif state.partial:
        detail = (
            f"{COMPAT_CHAIN} is only half there (chain: {state.chain_present}, jump: "
            f"{state.jump_present}, rules: {len(state.rules_present)} of "
            f"{len(state.rules_expected)}). Starting the node re-applies it."
        )
    else:
        detail = f"nodo has written nothing into the compatibility table for {bridge}."
    return CompatState(**{**state.__dict__, "detail": detail})


def ensure_compat(
    bridge: str,
    mode: CompatMode = CompatMode.AUTO,
    *,
    run: Optional[Runner] = None,
    log: Log = _silent,
) -> CompatState:
    """Put nodo's compatibility chain in place if this host needs it.

    Never raises and never fatal. A node that cannot write here still runs; what
    it must not do is pretend the traffic will get through. The caller logs, and
    ``nodo doctor`` proves it with a packet.
    """
    expected = tuple(rule.comment for rule in policy.compat_rules(bridge))

    if mode is CompatMode.OFF:
        return CompatState(
            mode=mode, rules_expected=expected,
            detail="virtualizers.ch.FORWARD_COMPAT is off, so nodo writes nothing "
                   "outside its own tables. If guests cannot reach each other, that "
                   "is where to look first.",
        )

    if not shutil.which("iptables"):
        return CompatState(
            mode=mode, available=False, rules_expected=expected,
            detail="There is no iptables on this host, so nothing writes a forward "
                   "policy into the compatibility table.",
        )

    needed: Optional[bool] = True
    if mode is CompatMode.AUTO:
        clear, why = _forward_hook_is_clear(run)
        if clear is True:
            return CompatState(
                mode=mode, rules_expected=expected, needed=False,
                detail=f"Leaving the compatibility table alone: {why}.",
            )
        needed = None if clear is None else True
        log(f"[FW] Applying the forward compatibility rules because {why}.")

    backend = _iptables_backend(run)
    changed = False
    try:
        changed |= backend.ensure_own_chain(Chain.FORWARD_COMPAT)
        for rule in policy.compat_rules(bridge):
            changed |= backend.ensure(rule)
        # The jump goes in last, so the chain is never reachable while half-filled.
        changed |= backend.ensure_first(policy.compat_jump_rule(COMPAT_CHAIN))
    except FirewallError as e:
        return CompatState(
            mode=mode, rules_expected=expected, needed=needed, error=str(e),
            detail=f"Could not put {COMPAT_CHAIN} in place. Guests may be unable to "
                   "reach each other; run 'nodo doctor' to see whether they can.",
        )
    except Exception as e:
        return CompatState(
            mode=mode, rules_expected=expected, needed=needed, error=repr(e),
            detail=f"Unexpected failure putting {COMPAT_CHAIN} in place.",
        )

    state = compat_state(bridge, mode, run=run)
    state = CompatState(**{**state.__dict__, "changed": changed, "needed": needed})
    if changed:
        log(f"[FW] {state.detail}")
    return state


def remove_compat(
    bridge: str,
    *,
    run: Optional[Runner] = None,
    log: Log = _silent,
) -> CompatState:
    """Take the hole back out: the jump first, then the chain and everything in it.

    The jump first is not cosmetic. iptables refuses to delete a chain something
    still references, so removing it in the other order leaves a jump pointing at a
    chain that no longer accepts anything -- which is the same as no rules at all,
    but silently and with nodo's name still on it.
    """
    expected = tuple(rule.comment for rule in policy.compat_rules(bridge))

    if not shutil.which("iptables"):
        return CompatState(
            mode=CompatMode.OFF, available=False, rules_expected=expected,
            detail="There is no iptables on this host; nothing to remove.",
        )

    backend = _iptables_backend(run)
    try:
        removed = backend.delete_by_comment(Chain.FORWARD, policy.COMPAT_JUMP_COMMENT)
        deleted_chain = backend.delete_own_chain(Chain.FORWARD_COMPAT)
    except FirewallError as e:
        return CompatState(
            mode=CompatMode.OFF, rules_expected=expected, error=str(e),
            detail=f"Could not remove {COMPAT_CHAIN}.",
        )

    if not removed and not deleted_chain:
        detail = "There was nothing of nodo's to remove from the compatibility table."
    else:
        detail = (
            f"Removed {COMPAT_CHAIN} and {removed} jump(s) into it. Guest traffic is "
            "now subject to whatever this host's FORWARD chain decides."
        )
    log(f"[FW] {detail}")
    # OFF describes the resulting state, not the configured key: after this, nodo
    # is not writing into the compatibility table. The key is untouched, so the
    # next launch re-applies the rules unless the operator also sets it.
    return CompatState(
        mode=CompatMode.OFF, rules_expected=expected,
        changed=bool(removed or deleted_chain), detail=detail,
    )
