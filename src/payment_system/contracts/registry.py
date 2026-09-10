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

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

contract_hash = str


@dataclass(frozen=True)
class MethodKey:
    """What identifies a payment method: a ledger, a contract **and an asset**.

    Not a contract. On Ergo one P2PK contract is paid in ERG *and* in every EIP-4 token
    held at the same address -- same script, same address, same ``contract_hash``,
    different money -- so a contract carries `1 + N` methods and only this triple tells
    them apart. On Bitcoin, and on a chain where a token *is* a contract (an ERC-20),
    the triple degenerates to one method per contract and costs nothing.

    Folding the asset into the contract type instead would have been cheaper and wrong:
    it claims the script differs when it does not, which is only true on Ethereum.
    """

    ledger: str
    contract_hash: str
    #: The chain's reserved symbol for its native unit ("ERG", "BTC"), or a token's
    #: 64-hex id -- which can never collide with a symbol.
    asset: str

    def __post_init__(self):
        """Normalise a token id's case, here and nowhere else.

        An id is 64 hex characters and explorers, config files and peers render it in
        either case, so `AB..` and `ab..` are the same asset -- but they are different
        dict keys, and a method keyed by one is simply not found by the other. The
        symptom is not a wrong payment, it is a peer that quietly becomes unpayable in
        that token, which is the kind of thing nobody debugs.

        Only an id is touched. A reserved native symbol ("ERG", "BTC") is left exactly
        as it is: it travels on the wire and in the database as advertised, and it
        cannot be 64 hex characters.
        """
        raw = str(self.asset or "")
        if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
            object.__setattr__(self, "asset", raw.lower())

    def __str__(self) -> str:
        return f"{self.ledger}/{self.contract_hash[:12]}/{self.asset or 'native'}"


class PaymentMethod:
    """One way to be paid: a contract with one asset bound to it.

    An *instance*, not a module, and that is the difference that makes `1 + N` methods
    per contract expressible at all. A module can only be one method; a contract that
    settles in several assets has to hand back one object per asset, with the asset
    already bound, so `process_payment` and `payment_process_validator` keep the
    signatures the rest of the flow calls them with.

    Everything not overridden is the contract's own: `LEDGER`, `NATIVE_ASSET`,
    `needs_unspent_proof`, and the per-contract jobs (`init`, `manager`). Only the calls
    whose answer depends on *which asset* are bound -- a fee floor, a balance, a rate --
    and a single-asset contract binds none of them and is forwarded to unchanged.
    """

    def __init__(self, contract, asset: str, calls: Optional[Dict[str, Any]] = None,
                 symbol: str = "", unit_name: str = ""):
        self.contract = contract
        self.asset = asset
        self._calls = dict(calls or {})
        # How a person names this asset, as opposed to what identifies it. A command
        # line is typed by someone reading a price, so `--asset sigusd` has to work --
        # but the id stays the identity, and the two cannot collide because an id is 64
        # hex characters. Defaults to the asset itself, which is right for a native
        # unit: "ERG" is both its symbol and its id.
        self.symbol = symbol or asset
        self.unit_name = unit_name or ""

    @property
    def key(self) -> MethodKey:
        return MethodKey(self.contract.LEDGER, self.contract.CONTRACT_HASH, self.asset)

    def __getattr__(self, name: str):
        """The bound call if this method has one, else the contract's own attribute.

        Reached only for names not set on the instance, so `contract`, `asset`, `key`,
        `symbol` and `unit_name` above shadow nothing. A missing member raises
        `AttributeError` from the contract, which is what `_usable` checks for up front.
        """
        calls = self.__dict__.get("_calls") or {}
        if name in calls:
            return calls[name]
        return getattr(self.__dict__["contract"], name)

    def __repr__(self) -> str:
        return f"<PaymentMethod {self.key}>"


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


def methods() -> Dict[MethodKey, PaymentMethod]:
    """Every payment method this node can settle through, keyed by its triple.

    A contract that settles in more than one asset says so by exposing ``methods()``;
    one that does not gets a single method bound to its native asset. Order follows the
    registry's candidate order and then each contract's own, because the payer tries
    candidates until one works and that walk has to be reproducible.
    """
    offered: Dict[MethodKey, PaymentMethod] = {}
    for contract in contracts().values():
        build = getattr(contract, "methods", None)
        if callable(build):
            try:
                built = list(build())
            except Exception as exc:
                _report(
                    contract.LEDGER,
                    f"Payment contract {contract.LEDGER!r} could not build its payment "
                    f"methods, so none of them is offered: {exc}",
                )
                continue
        else:
            built = [PaymentMethod(contract, getattr(contract, "NATIVE_ASSET", ""))]
        for method in built:
            offered[method.key] = method
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
