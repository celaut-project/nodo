"""Host-level netfilter primitives, independent of any virtualizer.

Two backends behind one interface:

* ``NftBackend``      -- native nftables, in nodo's own tables.
* ``IptablesBackend`` -- the legacy ``iptables`` binary.

``detect_backend()`` prefers nftables whenever the host actually speaks it,
because on such hosts ``iptables`` is the ``iptables-nft`` shim writing into the
compatibility ``ip filter``/``ip nat`` tables: the rules work, but they live where
nodo does not own them and stay invisible to an admin reading ``nft list
ruleset``. Mixing the two is worse than either, which is why every rule nodo
writes -- gateway port, guest egress policy, masquerade, published-port DNAT --
goes through here.

Tables and chains
-----------------
Filter lives in ``inet nodo`` (``input``, ``forward``, both at priority -5, ahead
of the standard filter chains). NAT lives in ``ip nodo`` (``prerouting`` at -100,
``postrouting`` at 100); it is a separate table because a table has exactly one
family and all of nodo's NAT is IPv4, which keeps us off the inet-NAT support
matrix entirely.

What ordering can and cannot buy
--------------------------------
``accept`` ends evaluation of its own chain only: the packet still traverses every
other base chain on the same hook, and a ``drop`` or ``reject`` there wins. So no
priority makes nodo's *accept* authoritative over a foreign reject -- an accept
rule is necessary but never sufficient, and the honest way to know a port is
reachable is to try it (see ``reachability``).

``drop`` is the opposite: it is terminal for the whole hook. So nodo's egress
policy -- the blanket NEW drop that isolates a guest -- becomes *stronger* here
than it was through the compatibility table, because it now runs at -5, ahead of
anything a host firewall might accept. That is the intended behaviour, and it is
the reason isolation must be verified rather than assumed too.

Nothing in this package may import ``src.utils.config`` or ``src.utils.logger``
(both build a ``ConfigManager``), because ``ConfigManager`` imports this package.
Callers pass their own values and their own ``log`` callable.
"""

import json
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.utils.firewall.errors import FirewallError, FirewallUnavailable
from src.utils.firewall.rules import Chain, Rule, Verdict

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

NFT_FILTER_FAMILY = "inet"
NFT_NAT_FAMILY = "ip"
NFT_TABLE = "nodo"

# Ahead of the standard filter chains (iptables-nft's compatibility table sits at
# 0, firewalld's at 10). See the module docstring on what this does and does not
# achieve.
NFT_FILTER_PRIORITY = -5

_NFT_CHAINS: Dict[Chain, Tuple[str, str, str]] = {
    Chain.INPUT: (
        NFT_FILTER_FAMILY,
        "input",
        f"type filter hook input priority {NFT_FILTER_PRIORITY} ; policy accept ;",
    ),
    Chain.FORWARD: (
        NFT_FILTER_FAMILY,
        "forward",
        f"type filter hook forward priority {NFT_FILTER_PRIORITY} ; policy accept ;",
    ),
    Chain.PREROUTING: (
        NFT_NAT_FAMILY,
        "prerouting",
        "type nat hook prerouting priority -100 ; policy accept ;",
    ),
    Chain.POSTROUTING: (
        NFT_NAT_FAMILY,
        "postrouting",
        "type nat hook postrouting priority 100 ; policy accept ;",
    ),
}

_IPTABLES_CHAINS: Dict[Chain, str] = {
    Chain.INPUT: "INPUT",
    Chain.FORWARD: "FORWARD",
    Chain.PREROUTING: "PREROUTING",
    Chain.POSTROUTING: "POSTROUTING",
}


@dataclass(frozen=True)
class ForeignRejector:
    """A base chain, not owned by nodo, that can reject inbound packets."""

    table: str
    chain: str
    priority: Optional[int]
    reason: str

    def __str__(self) -> str:
        priority = "?" if self.priority is None else str(self.priority)
        return f"{self.table} / {self.chain} (hook input, priority {priority}): {self.reason}"


@dataclass(frozen=True)
class AppliedRule:
    """A rule nodo owns, as read back from the live ruleset."""

    chain: Chain
    comment: str
    port: Optional[int] = None
    protocol: Optional[str] = None
    handle: Optional[int] = None
    tokens: Optional[Tuple[str, ...]] = None


# The gateway work predates the generic model and imports this name.
InputRule = AppliedRule


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


def _failure_text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip() or f"exit status {proc.returncode}"


def _means_absent(message: str) -> bool:
    """Whether a netfilter error means "no such object" rather than a fault."""
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in ("no such file", "does not exist", "no such table", "not exist")
    )


class FirewallBackend(ABC):
    """Apply, list and delete the rules nodo owns."""

    name: str = "unknown"

    def __init__(self, run: Optional[Runner] = None) -> None:
        self._run: Runner = run or _default_runner

    # --- primitives each backend provides ---------------------------------

    @abstractmethod
    def list_rules(self, chain: Chain, comment_prefix: str = "") -> List[AppliedRule]:
        """Rules in ``chain`` whose comment starts with ``comment_prefix``, in order."""

    @abstractmethod
    def add(self, rule: Rule) -> None:
        """Apply ``rule`` unconditionally, at the head or the tail per ``at_head``."""

    @abstractmethod
    def delete(self, applied: AppliedRule) -> None:
        """Delete a rule previously returned by ``list_rules``."""

    def foreign_input_rejectors(self) -> List[ForeignRejector]:
        """Base chains outside nodo's tables that could reject inbound packets.

        Best-effort diagnosis for the operator: never used to decide anything.
        """
        return []

    # --- the operations callers actually use -------------------------------

    def ensure(self, rule: Rule) -> bool:
        """Apply ``rule`` unless its comment is already present. True when added.

        Comments are unique per rule by construction, so this is exact.
        """
        for existing in self.list_rules(rule.chain, rule.comment):
            if existing.comment == rule.comment:
                return False
        self.add(rule)
        return True

    def ensure_first(self, rule: Rule) -> bool:
        """Apply ``rule`` and guarantee it is the first rule in its chain.

        Used for the blanket RELATED,ESTABLISHED accept, whose whole purpose is to
        short-circuit everything below it. Duplicates left by earlier versions are
        collapsed rather than tolerated.
        """
        existing = self.list_rules(rule.chain)
        matching = [applied for applied in existing if applied.comment == rule.comment]
        if len(matching) == 1 and existing and existing[0].comment == rule.comment:
            return False

        for applied in reversed(matching):
            self.delete(applied)
        self.add(Rule(**{**rule.__dict__, "at_head": True}))
        return True

    def delete_by_comment(self, chain: Chain, comment: str) -> int:
        """Delete every rule in ``chain`` carrying exactly ``comment``."""
        removed = 0
        for applied in reversed(self.list_rules(chain, comment)):
            if applied.comment != comment:
                continue
            self.delete(applied)
            removed += 1
        return removed

    def delete_by_comment_prefix(self, comment_prefix: str, chains: Sequence[Chain] = ()) -> int:
        """Delete every nodo rule under ``comment_prefix`` across ``chains``.

        How a VM's whole footprint is torn down: one prefix, every chain, no
        reliance on remembering the exact arguments that created each rule.
        """
        removed = 0
        for chain in chains or tuple(Chain):
            try:
                applied_rules = self.list_rules(chain, comment_prefix)
            except FirewallError:
                continue
            for applied in reversed(applied_rules):
                try:
                    self.delete(applied)
                    removed += 1
                except FirewallError:
                    continue
        return removed

    def prune_by_prefix(self, chain: Chain, comment_prefix: str, keep: str) -> List[AppliedRule]:
        """Drop rules under ``comment_prefix`` other than ``keep``.

        How a reassigned port stops leaving an orphan hole behind.
        """
        removed: List[AppliedRule] = []
        for applied in reversed(self.list_rules(chain, comment_prefix)):
            if applied.comment == keep:
                continue
            try:
                self.delete(applied)
                removed.append(applied)
            except FirewallError:
                continue
        return removed

    # --- gateway-port helpers (the input-hook special case) ----------------

    def list_input_accepts(self, comment_prefix: str) -> List[AppliedRule]:
        return self.list_rules(Chain.INPUT, comment_prefix)

    def ensure_input_accept(self, port: int, protocol: str, comment: str) -> bool:
        return self.ensure(
            Rule(
                chain=Chain.INPUT,
                comment=comment,
                verdict=Verdict.ACCEPT,
                protocol=protocol,
                dport=port,
                at_head=True,
            )
        )

    def remove_input_accept(self, rule: AppliedRule) -> None:
        self.delete(rule)

    def prune_input_accepts(self, comment_prefix: str, keep: str) -> List[AppliedRule]:
        return self.prune_by_prefix(Chain.INPUT, comment_prefix, keep=keep)


class NftBackend(FirewallBackend):
    name = "nftables"

    def __init__(self, run: Optional[Runner] = None) -> None:
        super().__init__(run=run)
        self._ready: set = set()

    def _nft(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["nft", *args])

    def _chain_spec(self, chain: Chain) -> Tuple[str, str, str]:
        try:
            return _NFT_CHAINS[chain]
        except KeyError:
            raise FirewallError(f"No nft chain defined for {chain}.")

    def _ensure_chain(self, chain: Chain) -> None:
        if chain in self._ready:
            return
        family, name, spec = self._chain_spec(chain)

        table = self._nft("add", "table", family, NFT_TABLE)
        if table.returncode != 0:
            raise FirewallError(
                f"Could not create nft table {family} {NFT_TABLE}: {_failure_text(table)}"
            )
        created = self._nft("add", "chain", family, NFT_TABLE, name, "{ " + spec + " }")
        if created.returncode != 0:
            raise FirewallError(f"Could not create nft chain {name}: {_failure_text(created)}")
        self._ready.add(chain)

    def list_rules(self, chain: Chain, comment_prefix: str = "") -> List[AppliedRule]:
        family, name, _ = self._chain_spec(chain)
        proc = self._nft("-j", "list", "chain", family, NFT_TABLE, name)
        if proc.returncode != 0:
            message = _failure_text(proc)
            if _means_absent(message):
                # nodo's table has not been created yet: nothing of ours exists.
                return []
            # Anything else -- notably a permission error -- must not be read as
            # "no rules", or ensure() would add a duplicate on every start.
            raise FirewallError(f"Could not list nft chain {name}: {message}")

        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError as e:
            raise FirewallError(f"Could not parse nft JSON output: {e}") from e

        found: List[AppliedRule] = []
        for item in data.get("nftables") or []:
            rule = item.get("rule") if isinstance(item, dict) else None
            if not isinstance(rule, dict):
                continue
            comment = rule.get("comment")
            if not isinstance(comment, str) or not comment.startswith(comment_prefix):
                continue
            port, protocol = _nft_port_match(rule.get("expr") or [])
            handle = rule.get("handle")
            found.append(
                AppliedRule(
                    chain=chain,
                    comment=comment,
                    port=port,
                    protocol=protocol,
                    handle=int(handle) if isinstance(handle, int) else None,
                )
            )
        return found

    def add(self, rule: Rule) -> None:
        self._ensure_chain(rule.chain)
        family, name, _ = self._chain_spec(rule.chain)
        verb = "insert" if rule.at_head else "add"
        # nft joins argv into one command string before parsing, so the whole
        # expression goes in as a single argument: that keeps the quoting of a
        # comment containing ';' unambiguous.
        proc = self._nft(verb, "rule", family, NFT_TABLE, name, _render_nft(rule))
        if proc.returncode != 0:
            raise FirewallError(
                f"Could not {verb} nft rule in {name} ({rule.comment}): {_failure_text(proc)}"
            )

    def delete(self, applied: AppliedRule) -> None:
        if applied.handle is None:
            raise FirewallError(f"Cannot delete an nft rule without a handle: {applied.comment}")
        family, name, _ = self._chain_spec(applied.chain)
        proc = self._nft(
            "delete", "rule", family, NFT_TABLE, name, "handle", str(applied.handle)
        )
        if proc.returncode != 0:
            raise FirewallError(
                f"Could not delete nft rule {applied.handle} in {name}: {_failure_text(proc)}"
            )

    def foreign_input_rejectors(self) -> List[ForeignRejector]:
        proc = self._nft("-j", "list", "ruleset")
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return []

        chains: Dict[tuple, dict] = {}
        for item in data.get("nftables") or []:
            if not isinstance(item, dict):
                continue
            chain = item.get("chain")
            if isinstance(chain, dict) and chain.get("hook") == "input":
                if chain.get("table") == NFT_TABLE and chain.get("family") == NFT_FILTER_FAMILY:
                    continue
                key = (chain.get("family"), chain.get("table"), chain.get("name"))
                chains[key] = {
                    "priority": chain.get("prio"),
                    "policy": chain.get("policy"),
                    "verdicts": set(),
                }

        for item in data.get("nftables") or []:
            if not isinstance(item, dict):
                continue
            rule = item.get("rule")
            if not isinstance(rule, dict):
                continue
            key = (rule.get("family"), rule.get("table"), rule.get("chain"))
            if key not in chains:
                continue
            for expr in rule.get("expr") or []:
                if not isinstance(expr, dict):
                    continue
                if "reject" in expr:
                    chains[key]["verdicts"].add("reject")
                if "drop" in expr:
                    chains[key]["verdicts"].add("drop")

        rejectors: List[ForeignRejector] = []
        for (family, table, name), info in chains.items():
            reasons = []
            if info["policy"] == "drop":
                reasons.append("chain policy is drop")
            for verdict in sorted(info["verdicts"]):
                reasons.append(f"contains a {verdict} rule")
            if not reasons:
                continue
            priority = info["priority"]
            rejectors.append(
                ForeignRejector(
                    table=f"{family} {table}",
                    chain=str(name),
                    priority=priority if isinstance(priority, int) else None,
                    reason=", ".join(reasons),
                )
            )
        rejectors.sort(key=lambda r: (r.priority if r.priority is not None else 0, r.table))
        return rejectors


def _render_nft(rule: Rule) -> str:
    """The nft expression for ``rule``, matches first, then verdict, then comment."""
    parts: List[str] = []
    if rule.source:
        parts.append(f"ip saddr {rule.source}")
    if rule.destination:
        operator = "!= " if rule.destination_is_negated else ""
        parts.append(f"ip daddr {operator}{rule.destination}")
    if rule.protocol and (rule.dport is not None or rule.sport is not None):
        if rule.dport is not None:
            parts.append(f"{rule.protocol} dport {rule.dport}")
        if rule.sport is not None:
            parts.append(f"{rule.protocol} sport {rule.sport}")
    elif rule.protocol:
        parts.append(f"meta l4proto {rule.protocol}")
    if rule.ct_states:
        parts.append("ct state " + ",".join(state.lower() for state in rule.ct_states))

    if rule.verdict is Verdict.DNAT:
        parts.append(f"dnat to {rule.dnat_to}")
    else:
        parts.append(rule.verdict.value)

    parts.append(f'comment "{rule.comment}"')
    return " ".join(parts)


def _nft_port_match(expressions: Sequence) -> Tuple[Optional[int], Optional[str]]:
    """Best-effort (port, protocol) of an nft rule, for reporting only."""
    port = None
    protocol = None
    for expr in expressions:
        if not isinstance(expr, dict):
            continue
        match = expr.get("match")
        if not isinstance(match, dict):
            continue
        left = match.get("left")
        if isinstance(left, dict):
            payload = left.get("payload")
            if isinstance(payload, dict) and payload.get("field") == "dport":
                right = match.get("right")
                if isinstance(right, int):
                    port = right
                protocol = payload.get("protocol") or protocol
    return port, protocol


class IptablesBackend(FirewallBackend):
    name = "iptables"

    def _iptables(self, chain: Chain, *args: str) -> subprocess.CompletedProcess:
        table = ["-t", "nat"] if chain.is_nat else []
        return self._run(["iptables", *table, *args])

    def list_rules(self, chain: Chain, comment_prefix: str = "") -> List[AppliedRule]:
        name = _IPTABLES_CHAINS[chain]
        proc = self._iptables(chain, "-S", name)
        if proc.returncode != 0:
            # Not "no rules": an unreadable chain must not be mistaken for an
            # empty one, or every start would insert another copy of the rule.
            raise FirewallError(f"Could not list the {name} chain: {_failure_text(proc)}")

        found: List[AppliedRule] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith(f"-A {name}"):
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            comment = _token_value(tokens, "--comment")
            if not comment or not comment.startswith(comment_prefix):
                continue
            port_text = _token_value(tokens, "--dport")
            try:
                port = int(port_text) if port_text else None
            except ValueError:
                port = None
            found.append(
                AppliedRule(
                    chain=chain,
                    comment=comment,
                    port=port,
                    protocol=_token_value(tokens, "-p"),
                    tokens=tuple(tokens),
                )
            )
        return found

    def add(self, rule: Rule) -> None:
        name = _IPTABLES_CHAINS[rule.chain]
        verb = "-I" if rule.at_head else "-A"
        proc = self._iptables(rule.chain, verb, name, *_render_iptables(rule))
        if proc.returncode != 0:
            raise FirewallError(
                f"Could not add iptables rule in {name} ({rule.comment}): {_failure_text(proc)}"
            )

    def delete(self, applied: AppliedRule) -> None:
        if not applied.tokens:
            raise FirewallError(
                f"Cannot delete an iptables rule without its own listing: {applied.comment}"
            )
        # Reconstructed from iptables' own -S output, so the delete matches the
        # rule exactly even when a previous nodo version wrote it differently.
        args = list(applied.tokens)
        args[0] = "-D"
        proc = self._iptables(applied.chain, *args)
        if proc.returncode != 0:
            raise FirewallError(
                f"Could not delete iptables rule ({applied.comment}): {_failure_text(proc)}"
            )


def _render_iptables(rule: Rule) -> List[str]:
    """The iptables match arguments for ``rule``.

    ``-p`` comes first because ``--dport``/``--sport`` are extensions of the
    protocol match and iptables rejects them otherwise.
    """
    args: List[str] = []
    if rule.protocol:
        args += ["-p", rule.protocol]
    if rule.source:
        args += ["-s", rule.source]
    if rule.destination:
        if rule.destination_is_negated:
            args += ["!", "-d", rule.destination]
        else:
            args += ["-d", rule.destination]
    if rule.dport is not None:
        args += ["--dport", str(rule.dport)]
    if rule.sport is not None:
        args += ["--sport", str(rule.sport)]
    if rule.ct_states:
        args += ["-m", "conntrack", "--ctstate", ",".join(rule.ct_states)]

    if rule.verdict is Verdict.DNAT:
        args += ["-j", "DNAT", "--to-destination", str(rule.dnat_to)]
    elif rule.verdict is Verdict.MASQUERADE:
        args += ["-j", "MASQUERADE"]
    else:
        args += ["-j", rule.verdict.value.upper()]

    args += ["-m", "comment", "--comment", rule.comment]
    return args


def _token_value(tokens: List[str], option: str) -> Optional[str]:
    for index in range(len(tokens) - 1):
        if tokens[index] == option:
            return tokens[index + 1]
    return None


def detect_backend(run: Optional[Runner] = None) -> FirewallBackend:
    """The backend this host actually speaks, nftables first.

    ``nft list tables`` is the probe rather than the mere presence of the binary:
    a host can ship ``nft`` while the kernel or the caller's privileges make it
    useless, and there the iptables path is the one that will work.
    """
    runner = run or _default_runner
    if shutil.which("nft"):
        proc = runner(["nft", "list", "tables"])
        if proc.returncode == 0:
            return NftBackend(run=runner)
    if shutil.which("iptables"):
        return IptablesBackend(run=runner)
    raise FirewallUnavailable(
        "Neither 'nft' nor 'iptables' is usable on this host. Install nftables "
        "(package 'nftables') or iptables before running the node."
    )
