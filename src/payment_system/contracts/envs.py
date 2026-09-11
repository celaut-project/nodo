"""The one place the payment flow asks "which contracts are there, and what can they do".

Every dict below used to be written out by hand with `ergo` named literally in it, six
times over, so a second payment system meant editing six functions plus everything that
read them. They are now one comprehension each over
:func:`src.payment_system.contracts.registry.contracts`, and nothing here names a ledger.

Two rules hold across all of them:

* **A contract that cannot settle is not offered.** The registry drops a ledger whose
  runtime is missing and says so once, so these dicts describe what this node can
  actually do rather than what it was configured to hope for.
* **Keyed by the payment method**, ``(ledger, contract, asset)`` -- not by contract and
  not by ledger. One ledger carries more than one contract, and on Ergo one contract
  carries more than one asset: the same script paid in ERG and in every EIP-4 token at
  the same address. Only the triple tells those apart.

Two families live here, and the split is not cosmetic:

* **Per method** -- what to validate with, what to pay with, what a floor is, what a
  rate is. All of it depends on which asset.
* **Per contract** -- `init`, the periodic `manager` job, its interval, a wallet
  balance. A tick dispatched per *asset* would run `N + 1` times and pay `N + 1` fees
  for one piece of work, and an Ergo transaction carries several assets in one output,
  so the job is the contract's.
"""
from importlib import import_module
from textwrap import dedent
from typing import Any, Callable, Dict, Optional, Tuple
from contextlib import nullcontext

from protos import celaut_pb2
from src.payment_system.contracts.registry import (
    MethodKey,
    attribute,
    contracts,
    methods,
    rate_modules,
)
from src.utils.logger import LOGGER

contract_hash = str
script = bytes
token = str
ledger = str
tx_id = str
amount = int
validate_token = Callable[[token], bool]
contract_ledger = celaut_pb2.Contract


def contract_for(hash_: contract_hash):
    """One registered contract by hash, or ``None`` when it is not offered.

    By contract, for the jobs that are per contract. Anything that depends on *which
    asset* is keyed by :class:`MethodKey` and comes from ``methods()``.
    """
    return contracts().get(hash_)


def method_for(key: MethodKey):
    """One registered payment method by its triple, or ``None``."""
    return methods().get(key)


def __getattr__(name: str):
    """``DEMOS`` as a live value rather than a snapshot taken at import.

    It used to be a module constant computed from `SIMULATE_PAYMENTS` the first time
    anything imported this module, which froze it against a config the operator may
    since have changed -- and made it depend on import order in tests. PEP 562 lets it
    stay spelled as an attribute while being answered from the registry.
    """
    if name == "DEMOS":
        return demos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def demos() -> Tuple[contract_hash, ...]:
    """Contracts that settle on no chain, so a payer must not look for a ledger.

    On the *payer's* side only: a simulated contract is never advertised, because a peer
    that read it out of `GetPeerInfo` and paid through it would have paid into nothing.
    """
    return tuple(
        key for key, method in methods().items()
        if attribute(method, "is_demo")
    )


def payment_process_validators() -> Dict[contract_hash, validate_token]:
    return {
        key: method.payment_process_validator
        for key, method in methods().items()
    }


def available_payment_process() -> Dict[contract_hash, Callable[[amount, token, ledger, script], contract_ledger]]:
    return {
        key: method.process_payment
        for key, method in methods().items()
    }


def check_sender_balances() -> Dict[contract_hash, Callable[[amount], bool]]:
    return {
        key: method.check_sender_balance
        for key, method in methods().items()
    }


def settlement_floors() -> Dict[MethodKey, Callable[[], Tuple[amount, amount]]]:
    """Per method: ``(fee, smallest payable output)`` in MU.

    Both figures in MU, and that promise is load-bearing rather than tidy: a token
    method's fee is paid in the chain's *native* unit while its minimum output is one
    base unit of the token, so the two are denominated in different assets. MU is the
    one scale that can carry both, and it is what `deposits.py` consumes.

    What a deposit has to clear before it can be settled at all. Kept here with the rest
    of the per-contract dispatch so `deposits.py` can size a deposit without naming a
    ledger -- it used to import Ergo's `DEFAULT_FEE` and `SAFE_MIN_BOX_VALUE` directly,
    which put a chain-specific floor on every payment system, including the simulated one.

    Read per call, never cached: a chain whose fee is a market rate rather than a
    constant reports a moving number here.
    """
    return {
        key: method.settlement_floors_mu
        for key, method in methods().items()
    }


def init_interfaces() -> Dict[contract_hash, Callable[[], None]]:
    return {hash_: contract.init for hash_, contract in contracts().items()}


def manage_interfaces() -> Dict[contract_hash, Callable[[], None]]:
    return {hash_: contract.manager for hash_, contract in contracts().items()}


def resolve_method(ledger_tag: str, hash_: contract_hash, asset: str = ""):
    """The registered method a proved payment names, or ``None`` if there is none.

    An unnamed asset resolves to the contract's *native* one: a payer that advertises no
    ``token_id`` is paying the chain's own unit, the only thing it can be paying. That
    is resolved here rather than carried through empty, because a debt accrued under the
    empty string is one no payout ever looks for -- it would grow for ever and never be
    paid, and nothing would raise.

    Only donations resolve a method this way, and what they take from it is
    ``mu_to_native``: a debt has to be *stored* in the asset it was incurred in, because
    one kept in MU would be retroactively reinterpreted the next time the operator
    changed that asset's rate. A contract that settles on no chain exposes no converter
    at all, rather than a zero one -- a share of a simulated payment is not a debt to
    anybody, and accruing one would have the node donating real funds against payments
    it never received.
    """
    registered = methods()
    if asset:
        return registered.get(MethodKey(ledger_tag, hash_, asset))
    for key, method in registered.items():
        if key.contract_hash == hash_ and key.asset == getattr(method, "NATIVE_ASSET", ""):
            return method
    return None


#: Where each ledger's donation scanner lives. Import paths rather than modules, so this
#: file names no chain's package at import time and a ledger whose scanner will not
#: import contributes nothing instead of taking the others down with it.
DONATION_SCAN_MODULES = (
    "src.payment_system.contracts.ergo.donation_scan",
)


def donation_scanners() -> Dict[ledger, Any]:
    """Per LEDGER: how to read donations paid to an address on that chain.

    Keyed by ledger tag rather than by contract or by method, unlike every other
    dispatch in this file, and that is not an inconsistency: a counted donation wallet
    is a property of the *chain*. One address receives whatever is sent to it, through
    any contract and in any asset, so there is exactly one way to read what reached it.

    Only the light scanning modules are imported, never a contract interface: indexing
    what other nodes donated is a read, and it must not need a wallet, a signature or a
    JVM. Each is tried on its own -- a payment stack that will not import contributes no
    scanner rather than raising, so a node with no Java still routes and simply counts
    no donations, and a second ledger's scanner is unaffected by the first's absence.
    """
    scanners: Dict[ledger, Any] = {}
    for path in DONATION_SCAN_MODULES:
        try:
            module = import_module(path)
        except Exception as exc:
            LOGGER(f"No donation scanner from {path}: {exc}")
            continue
        tag = getattr(module, "LEDGER", "")
        if tag:
            scanners[tag] = module
    return scanners


def seconds_per_block(ledger_tag: str) -> int:
    """How long a block takes on ``ledger_tag``; 0 when this node cannot say.

    Donation age is measured in seconds, not blocks: an Ergo block is ~120 s and a
    Bitcoin block ~600 s, so an age scale in blocks would weigh the same old donation
    five times differently depending on the chain it was paid on.
    """
    scanner = donation_scanners().get(ledger_tag)
    return int(getattr(scanner, "SECONDS_PER_BLOCK", 0) or 0)


def needs_unspent_proof() -> Tuple[contract_hash, ...]:
    """Contracts whose validator needs an output still to be unspent.

    Ergo proves an incoming payment by finding an unspent box carrying the deposit token
    in R4, so a sweep that consumes that box turns an honest payment into a rejected one
    -- which is why deposit generation is paused while one is in flight. A chain that
    proves payment from a confirmed transaction needs no pause and must not be dragged
    into one: its confirmations can take longer than the whole wait is bounded by.
    """
    # By *contract*, because what it gates is the contract's periodic job: the sweep
    # that would spend the box its own validator has to find unspent. A contract needs
    # the pause if any of its methods does.
    return tuple(
        hash_ for hash_, contract in contracts().items()
        if attribute(contract, "needs_unspent_proof")
    )


def manager_iteration_times() -> Dict[contract_hash, int]:
    """Per contract: how often its periodic job should run, in seconds.

    The orchestrator used to read one global figure out of **Ergo's** config block and
    apply it to everybody, which meant a node with no `ledgers.ergo` block could not
    import the payment orchestrator at all -- and that a second ledger's own interval,
    which it declares in its own block, was simply ignored.
    """
    times: Dict[contract_hash, int] = {}
    for hash_, contract in contracts().items():
        read = getattr(contract, "manager_iteration_time", None)
        try:
            times[hash_] = int(read()) if callable(read) else 0
        except Exception:
            times[hash_] = 0
    return {hash_: seconds for hash_, seconds in times.items() if seconds > 0}


def deposit_token_ttls() -> Dict[contract_hash, int]:
    """Per contract: how long a deposit token may sit unpaid before it is written off.

    On the contract rather than in the operator's config, because it describes how long
    that chain takes to confirm. A single global was an hour -- generous on Ergo, and
    short enough on a slow chain to reject an honest payment with the money already
    on-chain, which is the one direction an accounting error must never fall in.
    """
    return {
        key: int(attribute(method, "DEPOSIT_TOKEN_TTL"))
        for key, method in methods().items()
    }


def _reporting(hash_: Optional[MethodKey], hook: str, reporter):
    """A method's reporting context manager, or a no-op.

    Resolved per method because the payer enters it around *one* method's
    `process_payment`: with several payment methods, reporting through one that is not
    the one settling would attach a transaction id to the wrong payment.
    """
    method = methods().get(hash_) if hash_ else None
    factory = getattr(method, hook, None) if method else None
    if not callable(factory):
        return nullcontext()
    return factory(reporter)


def transaction_url_reporting(reporter, method: Optional[MethodKey] = None):
    """Report a submitted transaction's URL to the caller, for the given method."""
    return _reporting(method, "transaction_url_reporting", reporter)


def transaction_id_reporting(reporter, method: Optional[MethodKey] = None):
    """Report a submitted transaction's *id* to the caller, for the given method.

    Kept separate from the URL hook rather than folded into it. The URL is presentation
    -- `nodo pay` prints a link for a human to click -- while the id is the record: it
    is what `payments.tx_id` stores and what `tx_history` joins an explorer transaction
    back to a peer with. Recovering the id by parsing the URL would put link formatting
    in the accounting path, where a changed link would silently become a missing record.
    """
    return _reporting(contract_hash, "transaction_id_reporting", reporter)


def display_units() -> Dict[str, Dict[str, Any]]:
    """Display units the payment contracts contribute, keyed by unit name.

    Lets `monetary.display_unit` offer the operator the unit their payment system settles
    in without the accounting core naming a ledger. Each entry has the shape of a
    hand-declared `ui.UNITS.<name>` block, except that its rate is derived from the ledger
    and so cannot go stale.

    Only the *light* rate modules are imported, never a contract interface: this is
    reached from `format_mu`, which runs on log lines all over the node. A rate module
    that will not import contributes nothing rather than raising -- money still gets
    rendered, in raw MU. A rate that is present but *malformed* does raise: that is a
    configuration error, and quietly falling back to MU would hide it.

    Two contracts declaring the same unit name is a configuration error rather than a
    race between them: this is a `dict.update`, so the second would silently win and an
    operator's chosen display unit would mean whichever one imported last.
    """
    units: Dict[str, Dict[str, Any]] = {}
    for path in rate_modules():
        try:
            module = import_module(path)
        except (ImportError, ModuleNotFoundError, OSError):
            continue
        contributed = module.display_units()
        clashes = set(contributed) & set(units)
        if clashes:
            raise ValueError(
                f"Two payment systems both declare the display unit(s) "
                f"{', '.join(sorted(clashes))}. A unit name has to say which money it "
                "is, so rename one in its rate module."
            )
        units.update(contributed)
    return units


def print_payment_info() -> str:
    """One block per configured contract, for `nodo info`.

    One block each rather than a total: two payment systems are two balances in two
    places, and adding them up would name a figure the operator cannot spend.
    """
    blocks = []
    for contract in contracts().values():
        if attribute(contract, "is_demo"):
            continue
        get_balance = getattr(contract, "get_balance", None)
        if not callable(get_balance):
            continue
        try:
            address, balance = get_balance()
        except Exception as e:
            blocks.append(f"{contract.LEDGER}: wallet unavailable ({e}) \n")
            continue
        # The unit is the contract's own reserved symbol for its native asset, so a
        # figure is never printed without saying what it counts.
        unit = getattr(contract, "NATIVE_ASSET", "")
        block = f"{contract.LEDGER}: Wallet: {address}, Amount: {balance} {unit}".rstrip() + " \n"
        cold = getattr(contract, "COLD_WALLET", None)
        cold_address = cold() if callable(cold) else None
        # Not repeated when it *is* the wallet: a read-only Bitcoin backend is paid at
        # its cold wallet, and printing one address on two lines reads as two wallets.
        if cold_address and cold_address != address:
            block += f"{contract.LEDGER}: Cold Wallet: {cold_address} \n"
        blocks.append(block)
    if not blocks:
        return dedent(
            """\
            No payment system is available, so nobody can pay this node. Configure a
            ledger under `ledgers:` and check that its runtime is reachable.
            """
        )
    return "".join(blocks)
