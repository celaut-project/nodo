"""Host-level netfilter primitives, independent of any virtualizer.

Two backends behind one interface:

* ``NftBackend``      -- native nftables, in nodo's own ``inet nodo`` table.
* ``IptablesBackend`` -- the legacy ``iptables`` binary.

``detect_backend()`` prefers nftables whenever the host actually speaks it,
because on such hosts ``iptables`` is the ``iptables-nft`` shim writing into the
compatibility ``ip filter`` table: the rules work, but they live in a table nodo
does not own and stay invisible to an admin reading ``nft list ruleset``.

What no backend can do
----------------------
A rule inserted here cannot override a *later* base chain that rejects the same
packet. In nftables ``accept`` ends evaluation of its own chain only; the packet
then traverses every other base chain registered on the same hook, and a ``drop``
or ``reject`` there still wins. Chain priority changes the order, not that
outcome -- so no priority makes our accept authoritative over a foreign
reject. An accept rule is necessary but never sufficient, and the only honest
way to know a port is reachable is to try it: see ``reachability``.

Nothing in this package may import ``src.utils.config`` or ``src.utils.logger``
(both of which build a ``ConfigManager``), because ``ConfigManager`` imports
this package. Callers pass their own values and their own ``log`` callable.
"""

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

NFT_FAMILY = "inet"
NFT_TABLE = "nodo"
NFT_CHAIN = "input"
# Evaluated before the standard filter chains (iptables-nft's `ip filter` sits at
# 0, firewalld's `inet firewalld` at 10). This buys ordering and nothing else --
# see the module docstring on why ordering cannot defeat a foreign reject.
NFT_CHAIN_PRIORITY = -5

SUPPORTED_PROTOCOLS = ("tcp", "udp")


class FirewallError(Exception):
    """A netfilter operation failed."""


class FirewallUnavailable(FirewallError):
    """Neither nft nor iptables can be used on this host."""


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
class InputRule:
    """An input-accept rule owned by nodo."""

    comment: str
    port: Optional[int]
    protocol: Optional[str]
    handle: Optional[int] = None
    raw: Optional[str] = None


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


def _validate(port: int, protocol: str) -> None:
    if not isinstance(port, int) or isinstance(port, bool):
        raise FirewallError(f"Port must be an int, got {port!r}.")
    if not 1 <= port <= 65535:
        raise FirewallError(f"Port {port} is outside 1-65535.")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise FirewallError(f"Unsupported protocol {protocol!r}. Supported: {', '.join(SUPPORTED_PROTOCOLS)}.")


def _failure_text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip() or f"exit status {proc.returncode}"


def _means_absent(message: str) -> str:
    """Whether a netfilter error means "that object does not exist" rather than a fault."""
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in ("no such file", "does not exist", "no such table", "not exist")
    )


class FirewallBackend(ABC):
    """Manage nodo-owned accept rules on the input hook."""

    name: str = "unknown"

    def __init__(self, run: Optional[Runner] = None) -> None:
        self._run: Runner = run or _default_runner

    @abstractmethod
    def list_input_accepts(self, comment_prefix: str) -> List[InputRule]:
        """Every nodo-owned input rule whose comment starts with ``comment_prefix``."""

    @abstractmethod
    def add_input_accept(self, port: int, protocol: str, comment: str) -> None:
        """Insert an accept rule. Must not be called when one already exists."""

    @abstractmethod
    def remove_input_accept(self, rule: InputRule) -> None:
        """Delete a rule previously returned by ``list_input_accepts``."""

    def foreign_input_rejectors(self) -> List[ForeignRejector]:
        """Base chains outside nodo's own table that could reject inbound packets.

        Best-effort diagnosis for the operator: never used to decide anything.
        """
        return []

    def ensure_input_accept(self, port: int, protocol: str, comment: str) -> bool:
        """Idempotently ensure the accept rule exists. True when it was added now."""
        _validate(port, protocol)
        for rule in self.list_input_accepts(comment):
            if rule.comment == comment:
                return False
        self.add_input_accept(port=port, protocol=protocol, comment=comment)
        return True

    def prune_input_accepts(self, comment_prefix: str, keep: str) -> List[InputRule]:
        """Drop nodo-owned rules under ``comment_prefix`` other than ``keep``.

        This is how a reassigned port stops leaving an orphan hole behind.
        """
        removed: List[InputRule] = []
        for rule in self.list_input_accepts(comment_prefix):
            if rule.comment == keep:
                continue
            try:
                self.remove_input_accept(rule)
                removed.append(rule)
            except FirewallError:
                continue
        return removed


class NftBackend(FirewallBackend):
    name = "nftables"

    def _nft(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["nft", *args])

    def _nft_json(self, *args: str) -> dict:
        proc = self._run(["nft", "-j", *args])
        if proc.returncode != 0:
            raise FirewallError(f"nft -j {' '.join(args)} failed: {_failure_text(proc)}")
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError as e:
            raise FirewallError(f"Could not parse nft JSON output: {e}") from e

    def _ensure_chain(self) -> None:
        table = self._nft("add", "table", NFT_FAMILY, NFT_TABLE)
        if table.returncode != 0:
            raise FirewallError(f"Could not create nft table {NFT_FAMILY} {NFT_TABLE}: {_failure_text(table)}")
        spec = (
            "{ type filter hook input priority "
            f"{NFT_CHAIN_PRIORITY}"
            " ; policy accept ; }"
        )
        chain = self._nft("add", "chain", NFT_FAMILY, NFT_TABLE, NFT_CHAIN, spec)
        if chain.returncode != 0:
            raise FirewallError(f"Could not create nft chain {NFT_CHAIN}: {_failure_text(chain)}")

    def list_input_accepts(self, comment_prefix: str) -> List[InputRule]:
        proc = self._run(["nft", "-j", "list", "chain", NFT_FAMILY, NFT_TABLE, NFT_CHAIN])
        if proc.returncode != 0:
            message = _failure_text(proc)
            if _means_absent(message):
                # nodo's table has not been created yet: nothing of ours exists.
                return []
            # Anything else -- notably a permission error -- must not be read as
            # "no rules", or ensure_input_accept would add a duplicate every start.
            raise FirewallError(f"Could not list nodo's nft chain: {message}")
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError as e:
            raise FirewallError(f"Could not parse nft JSON output: {e}") from e

        rules: List[InputRule] = []
        for item in data.get("nftables") or []:
            rule = item.get("rule") if isinstance(item, dict) else None
            if not isinstance(rule, dict):
                continue
            comment = rule.get("comment")
            if not isinstance(comment, str) or not comment.startswith(comment_prefix):
                continue
            port, protocol = _nft_rule_match(rule.get("expr") or [])
            handle = rule.get("handle")
            rules.append(
                InputRule(
                    comment=comment,
                    port=port,
                    protocol=protocol,
                    handle=int(handle) if isinstance(handle, int) else None,
                )
            )
        return rules

    def add_input_accept(self, port: int, protocol: str, comment: str) -> None:
        _validate(port, protocol)
        self._ensure_chain()
        # nft joins argv into one command string before parsing, so the whole
        # expression goes in as a single argument: that keeps the quoting of a
        # comment containing ';' unambiguous.
        expression = f'{protocol} dport {port} accept comment "{comment}"'
        proc = self._nft("add", "rule", NFT_FAMILY, NFT_TABLE, NFT_CHAIN, expression)
        if proc.returncode != 0:
            raise FirewallError(f"Could not add nft accept rule for port {port}: {_failure_text(proc)}")

    def remove_input_accept(self, rule: InputRule) -> None:
        if rule.handle is None:
            raise FirewallError(f"Cannot delete nft rule without a handle: {rule.comment}")
        proc = self._nft(
            "delete", "rule", NFT_FAMILY, NFT_TABLE, NFT_CHAIN, "handle", str(rule.handle)
        )
        if proc.returncode != 0:
            raise FirewallError(f"Could not delete nft rule {rule.handle}: {_failure_text(proc)}")

    def foreign_input_rejectors(self) -> List[ForeignRejector]:
        try:
            data = self._nft_json("list", "ruleset")
        except FirewallError:
            return []

        chains: dict = {}
        for item in data.get("nftables") or []:
            if not isinstance(item, dict):
                continue
            chain = item.get("chain")
            if isinstance(chain, dict) and chain.get("hook") == "input":
                table = chain.get("table")
                if table == NFT_TABLE and chain.get("family") == NFT_FAMILY:
                    continue
                key = (chain.get("family"), table, chain.get("name"))
                policy = chain.get("policy")
                chains[key] = {
                    "priority": chain.get("prio"),
                    "policy": policy,
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


def _nft_rule_match(expressions: Sequence) -> tuple:
    """Best-effort (port, protocol) of an nft accept rule, for reporting only."""
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

    def _iptables(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["iptables", *args])

    def _rule_args(self, port: int, protocol: str, comment: str) -> List[str]:
        return [
            "-p", protocol,
            "--dport", str(port),
            "-j", "ACCEPT",
            "-m", "comment",
            "--comment", comment,
        ]

    def list_input_accepts(self, comment_prefix: str) -> List[InputRule]:
        proc = self._iptables("-S", "INPUT")
        if proc.returncode != 0:
            # Not "no rules": an unreadable chain must not be mistaken for an
            # empty one, or every start would insert another copy of the rule.
            raise FirewallError(f"Could not list the INPUT chain: {_failure_text(proc)}")

        rules: List[InputRule] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("-A INPUT"):
                continue
            comment = _iptables_option(line, "--comment")
            if not comment or not comment.startswith(comment_prefix):
                continue
            port_text = _iptables_option(line, "--dport")
            protocol = _iptables_option(line, "-p")
            try:
                port = int(port_text) if port_text else None
            except ValueError:
                port = None
            rules.append(
                InputRule(comment=comment, port=port, protocol=protocol, raw=line)
            )
        return rules

    def add_input_accept(self, port: int, protocol: str, comment: str) -> None:
        _validate(port, protocol)
        proc = self._iptables("-I", "INPUT", *self._rule_args(port, protocol, comment))
        if proc.returncode != 0:
            raise FirewallError(
                f"Could not add iptables accept rule for port {port}: {_failure_text(proc)}"
            )

    def remove_input_accept(self, rule: InputRule) -> None:
        if rule.port is None or not rule.protocol:
            raise FirewallError(f"Cannot delete iptables rule without port/protocol: {rule.comment}")
        proc = self._iptables(
            "-D", "INPUT", *self._rule_args(rule.port, rule.protocol, rule.comment)
        )
        if proc.returncode != 0:
            raise FirewallError(f"Could not delete iptables rule for port {rule.port}: {_failure_text(proc)}")


def _iptables_option(line: str, option: str) -> Optional[str]:
    """Value following ``option`` in an ``iptables -S`` line, unquoting comments."""
    try:
        tokens = _split_iptables_line(line)
    except ValueError:
        return None
    for index in range(len(tokens) - 1):
        if tokens[index] == option:
            return tokens[index + 1]
    return None


def _split_iptables_line(line: str) -> List[str]:
    import shlex

    return shlex.split(line)


def detect_backend(run: Optional[Runner] = None) -> FirewallBackend:
    """The backend this host actually speaks, nftables first.

    ``nft list tables`` is the probe rather than the mere presence of the binary:
    a host can ship ``nft`` while the kernel or the caller's privileges make it
    useless, and in that case the iptables path is the one that will work.
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
