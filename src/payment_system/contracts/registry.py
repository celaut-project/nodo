"""Which payment contracts this node has, and what one has to look like.

`envs.py` used to write out the same pair of contracts by hand, six times, with `ergo`
named literally in every dict. The seam was real -- `deposits.py` refuses to name a
ledger, `monetary.py` refuses to know what an MU is worth -- but nothing had ever been
pushed through it, so "nodo allows the simultaneous use of multiple ledgers"
(`docs/ERGO.md`, first line) was aspiration rather than fact.

This is the list. A contract is a *module* satisfying :class:`PaymentContract`, and it is
offered only when its ledger is configured **and** its runtime is actually there. Both
gates fail the same way: the contract is not offered, and the node says so once. A
payment system that cannot work must not be advertised -- a peer that reads it from
`GetPeerInfo` and pays through it has paid into nothing.

Order is declaration order and it is deliberate. The payer tries candidates until one
works (issue #340 §4.4), so a set-ordered registry would make *which* system settles a
payment depend on dictionary iteration order -- reproducible until the day it is not.
"""
from __future__ import annotations

from importlib import import_module
from typing import Dict, List, Optional, Protocol, Tuple, runtime_checkable

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

contract_hash = str


@runtime_checkable
class PaymentContract(Protocol):
    """What every payment contract module has to expose.

    Structural, not inherited: a contract is a module, and this is the shape
    ``envs.py`` dispatches against. A module missing one of these does not fail at
    import -- it fails on the payment that needed it, which is the worst moment -- so
    the registry checks the shape up front and refuses to offer a contract that does
    not have it.

    The ``rate`` sibling module is deliberately *not* here: ``display_units`` is read
    from ``format_mu``, which runs on log lines all over the node, so it must be
    reachable without importing a payment stack (``monetary.py``). The registry exposes
    it separately, by module path.
    """

    #: Stable, wallet-independent identity of the contract TYPE. Its sha3 is the
    #: ``contract_hash`` peers match on; the per-instance wallet script travels apart.
    CONTRACT: str
    CONTRACT_HASH: str
    LEDGER: str

    def ledger(self) -> "celaut_pb2.Contract.Ledger": ...
    def init(self) -> None: ...
    def manager(self) -> None: ...
    def process_payment(self, amount: int, deposit_token: str, ledger, script: bytes): ...
    def payment_process_validator(self, amount: int, token: str, ledger, script: bytes) -> bool: ...
    def check_sender_balance(self, amount: int) -> bool: ...
    def settlement_floors_mu(self) -> Tuple[int, int]: ...


# What a contract module must define to be offered at all. Checked by name rather than
# with `isinstance`, because a module is not an instance of a Protocol and because the
# error worth printing names the missing member.
REQUIRED_MEMBERS = (
    "CONTRACT",
    "CONTRACT_HASH",
    "LEDGER",
    "ledger",
    "init",
    "manager",
    "process_payment",
    "payment_process_validator",
    "check_sender_balance",
    "settlement_floors_mu",
)

# Optional members, with the default the registry assumes when a contract omits them.
#
# `needs_unspent_proof` is the one that matters: Ergo proves an incoming payment by
# finding an *unspent* box carrying the deposit token, so no sweep may run while a
# deposit is in flight. A chain that proves payment from a confirmed transaction needs
# no such pause and must not be dragged into one (#340 §4.3).
#
# `unavailable_reason` is the cheap "can this settle right now" probe: it returns None
# when the contract is usable, or a sentence saying what is missing. A contract that
# omits it is assumed usable once it imports.
OPTIONAL_DEFAULTS = {
    "needs_unspent_proof": False,
    "is_demo": False,
    #: How long a deposit token may sit unpaid before it is written off. A property of
    #: how long the chain takes to confirm, which the contract knows and an operator
    #: should not have to state.
    "DEPOSIT_TOKEN_TTL": 3600,
}


class _Candidate:
    """One contract this build knows how to offer, and when it may be offered."""

    def __init__(self, name: str, module_path: str, rate_path: Optional[str] = None):
        self.name = name
        self.module_path = module_path
        self.rate_path = rate_path


# Declaration order is candidate order. The simulated contract comes first so a node
# running with `SIMULATE_PAYMENTS` settles through it rather than through a real chain
# it also happens to have configured.
CANDIDATES: Tuple[_Candidate, ...] = (
    _Candidate("simulated", "src.payment_system.contracts.simulator.interface"),
    _Candidate(
        "ergo",
        "src.payment_system.contracts.ergo.interface",
        "src.payment_system.contracts.ergo.rate",
    ),
    _Candidate(
        "bitcoin",
        "src.payment_system.contracts.bitcoin.interface",
        "src.payment_system.contracts.bitcoin.rate",
    ),
)

# Said once per process per contract, not per payment: this is reached from the payment
# path and from every advertisement.
_reported: set = set()


def _report(name: str, message: str) -> None:
    if name in _reported:
        return
    _reported.add(name)
    LOGGER(message)


def forget_reports() -> None:
    """Let the registry complain again. For a test, and for a config reload."""
    _reported.clear()


def _configured(name: str) -> bool:
    """Whether the operator has asked for this contract at all.

    The simulated contract is a flag; a real one is a `ledgers.<name>` block. A ledger
    nobody configured is not an error and is not reported -- it is simply not offered.
    """
    config = ConfigManager()
    if name == "simulated":
        return bool(config.get("general_flags.SIMULATE_PAYMENTS"))
    return isinstance(config.get(f"ledgers.{name}"), dict)


def _usable(module, name: str) -> bool:
    missing = [member for member in REQUIRED_MEMBERS if not hasattr(module, member)]
    if missing:
        _report(
            name,
            f"Payment contract {name!r} is not being offered: its module does not "
            f"define {', '.join(missing)}. This is a bug in the contract, not a "
            "configuration problem.",
        )
        return False
    return True


def contracts() -> Dict[contract_hash, PaymentContract]:
    """Every contract this node can actually settle through, keyed by contract hash.

    Never raises. A ledger whose runtime is missing -- no JVM, no reachable node, an
    unset rate -- contributes nothing, because a contract that cannot settle must not be
    advertised: a peer that reads it out of `GetPeerInfo` and pays through it has paid
    into nothing.
    """
    offered: Dict[contract_hash, PaymentContract] = {}
    for candidate in CANDIDATES:
        if not _configured(candidate.name):
            continue
        try:
            module = import_module(candidate.module_path)
        except Exception as exc:
            # Includes the JVM being absent, which surfaces as ImportError/OSError from
            # ergpy, and a chain module refusing to load because its rate is unset.
            _report(
                candidate.name,
                f"Payment contract {candidate.name!r} is configured but not available, "
                f"so this node is not offering it: {exc}",
            )
            continue
        if not _usable(module, candidate.name):
            continue
        # A contract that imports but cannot settle -- no Java for Ergo, no rate or no
        # RPC for Bitcoin -- is not offered either. The check has to be *cheap*: this
        # runs on the payment path and on every advertisement, so it may look at the
        # config and the filesystem and must not start a runtime or dial a node.
        reason = getattr(module, "unavailable_reason", None)
        unavailable = reason() if callable(reason) else None
        if unavailable:
            _report(
                candidate.name,
                f"Payment contract {candidate.name!r} is configured but not usable, so "
                f"this node is not offering it: {unavailable}",
            )
            continue
        offered[module.CONTRACT_HASH] = module
    return offered


def rate_modules() -> List[str]:
    """Import paths of the *light* rate modules, in candidate order.

    Paths rather than modules, so a caller on the `format_mu` path imports them itself
    and pulls in nothing else. A contract with no rate module contributes no display
    unit, which is the honest answer for one that settles on no chain.
    """
    return [c.rate_path for c in CANDIDATES if c.rate_path]


def attribute(module, name: str):
    """One optional member of a contract, or the registry's default for it."""
    return getattr(module, name, OPTIONAL_DEFAULTS.get(name))
