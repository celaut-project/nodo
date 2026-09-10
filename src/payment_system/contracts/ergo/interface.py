from src.reputation_system.envs import ergo_ledger
from decimal import Decimal
from typing import Optional, Tuple
from protos import celaut_pb2
import requests
from hashlib import sha3_256
from src.database import sql_connection
from src.payment_system.exceptions import DoubleSpendingAttempt
from src.payment_system.sweeps import compute_sweep_amount as _compute_sweep_amount
from src.utils.logger import LOGGER
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_address, set_script, set_token_id, set_contract_type
from src.utils.ergo_units import erg_to_nanoerg, is_valid_ergo_address, nanoerg_to_erg_str
# This ledger's MU rate and its conversions. A separate, light module on purpose: it is
# also what `monetary.display_unit` resolves ERG through, and that runs on log lines, so
# it must not pull in everything below.
from src.payment_system.contracts.ergo import rate
from src.payment_system.contracts.ergo.ergo_tree import (
    ergo_contract_from_proposition_bytes,
    proposition_bytes_from_address,
)
from src.utils.java_dependency import JavaDependencyMissing, ensure_ergpy_jvm, require_java_module
from contextlib import contextmanager
from functools import partial
from contextvars import ContextVar
from threading import Lock
from time import sleep


def _ergo_runtime():
    ensure_ergpy_jvm(feature="Ergo payments")
    appkit = require_java_module("ergpy.appkit", feature="Ergo payments")
    helper_functions = require_java_module("ergpy.helper_functions", feature="Ergo payments")
    jpype = require_java_module("jpype", feature="Ergo payments")
    org_appkit = jpype.JPackage("org").ergoplatform.appkit
    return appkit, helper_functions.simple_send, jpype, org_appkit


# Initialize environment and global variables
env_manager = ConfigManager()
DEFAULT_FEE = 1_000_000  # Fee for the transaction in nanoErgs
# Technical minimum box value the node must always retain / be able to build an output with.
SAFE_MIN_BOX_VALUE = 1_000_000
LEDGER = "ergo"  # or "ergo-testnet" for Ergo testnet.
# Stable, wallet-independent identity of the Ergo P2PK payment contract TYPE. Its sha3 is
# the contract_hash used to match this kind of contract across nodes; the specific wallet
# ErgoTree travels per-instance as the raw ``script`` xattr (propositionBytes).
CONTRACT = "proveDlog(decodePoint())"
CONTRACT_HASH = sha3_256(CONTRACT.encode("utf-8")).hexdigest()
# How this ledger names its own native unit on the wire and in the database. A reserved
# symbol rather than an id: an EIP-4 token is named by its 64-hex id, which can never
# collide with this. It is what `init()` already advertises as the `token_id` xattr.
NATIVE_ASSET = "ERG"

# This contract proves an incoming payment by finding an *unspent* box carrying the
# deposit token in R4, so no sweep may spend that box while a deposit is in flight --
# `payment_process._pause_and_drain_deposits` exists for exactly this. A chain that
# proves payment from a confirmed transaction sets this False and is not paused.
needs_unspent_proof = True
# How long a deposit token may sit unpaid here before it is written off. Ergo confirms
# in ~2 minutes and `process_payment` waits for two confirmations, so an hour is not a
# deadline anyone meets by accident. It lives on the contract rather than in the config
# because it describes the chain, which the contract knows and the operator should not
# have to state.
DEPOSIT_TOKEN_TTL = 3600

# The node controls exactly ONE wallet. Clients pay directly to its P2PK address; excess is
# swept to the cold wallet (a public address, never a mnemonic in Nodo).
WALLET_MNEMONIC = lambda: env_manager.get("ledgers.ergo.WALLET_MNEMONIC")
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
COLD_WALLET = lambda: env_manager.get("ledgers.ergo.payments.COLD_WALLET") or ""
# Read per call, not captured at import, so a test (and the TUI's restart-into-a-new-
# config) sees the value the node is actually running with -- and by its explicit path,
# so the key has one unambiguous home rather than relying on `ConfigManager.get`'s
# scan-every-section fallback for a dotless name.
#
# Plain truthiness, matching the registry's own gate for the simulated contract
# exactly. That is deliberate: the two must never disagree about whether this node is
# simulating, and a stricter parse here would make them differ on a value one mis-reads.
SIMULATE_PAYMENTS = lambda: bool(env_manager.get("general_flags.SIMULATE_PAYMENTS"))

# Donations are a share of *earnings* and have nothing to do with this wallet's excess
# being swept to cold storage: they are accrued when a payment arrives and paid on the
# tick below, out of `donations.config`'s weighted wallet list. The single
# `DONATION_WALLET` key and the split of the sweep that used it are gone.


def _hot_wallet_limit_nanoerg() -> int:
    """Hot-wallet limit parsed ONCE from the ERG decimal string to integer nanoERG."""
    return erg_to_nanoerg(env_manager.get("ledgers.ergo.payments.HOT_WALLET_LIMITS"))


def _cold_wallet_min_transfer_nanoerg() -> int:
    """Cold-wallet minimum sweep amount parsed ONCE to integer nanoERG."""
    return erg_to_nanoerg(env_manager.get("ledgers.ergo.payments.COLD_WALLET_MIN_TRANSFER"))


WAIT_TX_TIME = 240  # (each 5 seconds)
WAT_TX_SLEEP_TIME = 5

payment_lock = Lock()  # Ensures the same input box is not spent for more than it holds.
_transaction_url_reporter: ContextVar = ContextVar(
    "ergo_transaction_url_reporter", default=None
)


_transaction_id_reporter: ContextVar = ContextVar(
    "ergo_transaction_id_reporter", default=None
)


@contextmanager
def transaction_url_reporting(reporter):
    """Temporarily report a submitted Ergo transaction URL to the caller."""
    token = _transaction_url_reporter.set(reporter)
    try:
        yield
    finally:
        _transaction_url_reporter.reset(token)


@contextmanager
def transaction_id_reporting(reporter):
    """Temporarily report a submitted transaction's *id* to the caller.

    Kept separate from the URL hook above rather than folded into it. The URL is
    presentation -- `nodo pay` prints a sigmaspace link for a human to click -- while
    the id is the record: it is what `payments.tx_id` stores and what `tx_history`
    joins an explorer transaction back to a peer with. Recovering the id by parsing
    the URL would put explorer-link formatting in the accounting path, where a
    changed link would silently become a missing payment record.
    """
    token = _transaction_id_reporter.set(reporter)
    try:
        yield
    finally:
        _transaction_id_reporter.reset(token)


def __mu_to_nanoerg(amount: int) -> int:
    """MU -> nanoERG, at this ledger's declared rate (see ``rate.py``, next to this file).

    The rate lives in ``ledgers.ergo.payments.MU_PER_NANOERG`` (1 by default, which makes
    the conversion the identity). It is the single point where the node's unit of account
    meets real money, and it is the same number peers are told as
    ``ContractRate.mu_per_unit``, so payer and receiver compute the same figure.

    The old `GAS_PER_ERG` did this with a float reciprocal set to 1e58, which silently
    turned every real charge into zero nanoERG.
    """
    return rate.mu_to_nanoerg(amount)


def manager_iteration_time() -> int:
    """How often this contract's periodic job should run, in seconds.

    Per contract, because the job is per contract: a chain whose fee moves wants its
    sweep considered more often than one whose fee is a constant. The generic loop used
    to read *Ergo's* key for everybody, which meant a node without an `ledgers.ergo`
    block could not even import the payment orchestrator.
    """
    return max(1, int(env_manager.get("ledgers.ergo.payments.PAYMENT_MANAGER_ITERATION_TIME", 86400) or 86400))


def unavailable_reason() -> Optional[str]:
    """Why this contract cannot settle right now, or ``None`` when it can.

    Cheap on purpose -- a filesystem check for a Java runtime, no JVM start and no
    network -- because the registry asks this on the payment path and on every
    advertisement. It is what stops a node without Java from advertising a payment
    method nobody can actually pay into: the failure used to surface at the first real
    payment, with a peer's money already committed to the attempt.

    A wallet-less config is *not* reported here: the mnemonic is validated at load
    (`utils.config_validation`), and a node mid-setup should not have its payment
    system disappear from its own logs for a reason config validation already gave.
    """
    from src.utils.java_dependency import ensure_java_runtime

    try:
        ensure_java_runtime(feature="Ergo payments")
    except JavaDependencyMissing as exc:
        return str(exc).strip().splitlines()[0] if str(exc).strip() else "Java is not installed"
    return None


def mu_per_unit() -> int:
    """MU bought by one **whole** unit of this ledger -- one ERG, not one nanoERG.

    What travels to peers as ``ContractRate.mu_per_unit``, and the only thing that makes
    a price quoted in MU actionable to whoever reads it. Whole units rather than base
    units because both sides convert through the same figure (``mu_conversion``): the
    convention only has to be *shared*, and a whole unit is the one a person can check.
    """
    return rate.mu_per_erg()


def ledger() -> celaut_pb2.Contract.Ledger:
    """The ledger message this contract settles on, as peers receive it.

    A function rather than the module-level object it returns, so the registry can ask
    every contract the same question without importing each one's constants.
    """
    return ergo_ledger


def mu_to_native(amount: int) -> Decimal:
    """MU -> nanoERG, exactly. What a donation debt on this ledger is counted in.

    The generic donation code reaches this through ``contracts.envs.mu_to_native``; it
    never names nanoERG, and this is where the name is answered.
    """
    return rate.mu_to_nanoerg_exact(amount)


def settlement_floors_mu() -> Tuple[int, int]:
    """``(fee, smallest payable output)`` for this ledger, in MU.

    The two hard limits any deposit has to clear on Ergo: every transaction pays a fee,
    and the network refuses an output below its technical minimum box value.

    Reported in MU rather than nanoERG because the caller sizing a deposit
    (``src/payment_system/deposits.py``) is ledger-agnostic and counts in MU; both
    constants here are Ergo's own and therefore nanoERG. The two coincide only while
    ``MU_PER_NANOERG`` is 1, so the conversion is explicit. A ledger with no fee and no
    minimum output reports ``(0, 0)`` and simply imposes no floor.
    """
    return rate.nanoerg_to_mu(DEFAULT_FEE), rate.nanoerg_to_mu(SAFE_MIN_BOX_VALUE)


def __nanoerg_to_erg(amount: int) -> float:
    return amount / 1_000_000_000


def __init_ergo():
    appkit, _, _, _ = _ergo_runtime()
    node_url = ERGO_NODE_URL()
    if not node_url.endswith('/'):
        node_url += '/'
    return appkit.ErgoAppKit(node_url=node_url)


def __get_sender_addr(mnemonic: str):
    ergo = __init_ergo()
    _m = ergo.getMnemonic(wallet_mnemonic=mnemonic, mnemonic_password=None)
    return ergo.getSenderAddress(index=0, wallet_mnemonic=_m[1], wallet_password=_m[2])


def __balance_total(address) -> Optional[dict]:
    ergo = __init_ergo()
    explorer_api = ergo.get_api_url()
    url = f"{explorer_api}/api/v1/addresses/{str(address.toString())}/balance/total"
    response = requests.get(url)
    if response.status_code != 200:
        LOGGER(f"Error fetching the total balance: {response.status_code} - {response.text}")
        return None
    return response.json()


def __confirmed_balance_nanoerg(address) -> int:
    total = __balance_total(address=address)
    if not total:
        return 0
    return int(total["confirmed"]["nanoErgs"])


def get_wallet_address() -> str:
    """Readable base58 address of the single wallet (UI/API/log boundary only)."""
    return str(__get_sender_addr(WALLET_MNEMONIC()).toString())


def get_wallet_proposition_bytes() -> bytes:
    """Raw P2PK propositionBytes (canonical ErgoTree) of the single wallet."""
    return proposition_bytes_from_address(get_wallet_address())


def get_amount_by_addr(mnemonic: str) -> int:
    """Confirmed balance in integer nanoERG of the wallet derived from ``mnemonic``."""
    return __confirmed_balance_nanoerg(__get_sender_addr(mnemonic=mnemonic))


def transaction_history(limit: int = 10) -> list:
    """Recent transactions at this node's wallet, normalised (see ``history.py``).

    Delegated to a light sibling so `nodo tx_history` needs no JVM: it is a read, and
    the command used to reach into this module's privates to do it.
    """
    from src.payment_system.contracts.ergo import history

    return history.transaction_history(get_wallet_address(), limit=limit)


def get_balance() -> Tuple[str, float]:
    """Return (address, confirmed balance in ERG) for the single wallet."""
    addr = __get_sender_addr(WALLET_MNEMONIC())
    return str(addr.toString()), __nanoerg_to_erg(__confirmed_balance_nanoerg(addr))


def init():
    """Advertise this contract's payment methods: ERG plus one per configured asset.

    One row per *method*, all sharing the script, the type and the address and differing
    only in ``token_id``. That is what an Ergo P2PK contract actually is: one script paid
    in the native unit and in every EIP-4 token held at the same address, so the address
    is not what tells the methods apart and the rate cannot live on one row for all of
    them. ERG's row is byte-identical to the one this always wrote, ``token_id: "ERG"``
    included.

    Per *contract* rather than per method: ``envs.init_interfaces`` is keyed by contract,
    so this runs once and registers all of them. There is one wallet and one address.
    """
    proposition_bytes = get_wallet_proposition_bytes()
    address = get_wallet_address()
    assets = rate.assets()
    if assets:
        _warn_if_tokens_cannot_be_moved(assets)
    sql = sql_connection.SQLConnection()
    for asset in (NATIVE_ASSET, *(a.token_id for a in assets)):
        contract = celaut_pb2.Contract(ledger=ergo_ledger)
        set_token_id(contract, asset)
        # Canonical value: raw ErgoTree/propositionBytes of the wallet's P2PK payment boxes.
        set_script(contract, proposition_bytes)
        # Stable type identity for cross-node matching (its sha3 == CONTRACT_HASH).
        set_contract_type(contract, CONTRACT.encode("utf-8"))
        # Derived address for local display/indexing only; never the source of truth.
        set_address(contract, address)
        sql.add_contract(contract=contract)


def _warn_if_tokens_cannot_be_moved(assets) -> None:
    """Say at startup what a token-only wallet will not be able to do.

    Being *paid* in a token needs no ERG at all -- the payer supplies the fee and the
    box the token travels in -- so a node can accept SigUSD with an empty ERG balance
    and be perfectly configured. Paying one, sweeping one to cold storage and donating
    one all need ERG of this node's own, and each of those failures otherwise surfaces
    much later and somewhere else: a payment that falls through to another method, a
    sweep that logs and returns, a donation debt that quietly keeps growing.

    Checked against the wallet rather than announced whenever assets are configured. An
    unconditional warning on a correct config is a warning operators learn to ignore,
    and this one is only true of a wallet that is actually short.
    """
    try:
        needed = DEFAULT_FEE + 2 * SAFE_MIN_BOX_VALUE
        held = __confirmed_balance_nanoerg(__get_sender_addr(WALLET_MNEMONIC()))
        if held >= needed:
            return
        LOGGER(
            f"This node accepts {len(assets)} token(s) on Ergo and its wallet holds "
            f"{nanoerg_to_erg_str(held)} ERG, less than the "
            f"{nanoerg_to_erg_str(needed)} a token transaction needs for its fee and "
            "its output box. It can still be PAID in those tokens -- the payer supplies "
            "both -- but it cannot pay in them, cannot sweep them to the cold wallet, "
            "and cannot pay their donations until it holds some ERG."
        )
    except Exception as e:
        # A wallet balance that cannot be read is not a reason to fail registration:
        # without the rows this writes, no peer can pay this node at all.
        LOGGER(f"Could not check the ERG balance behind the configured tokens: {e}")


def check_sender_balance(amount: int) -> bool:
    try:
        # The transaction also has to cover its own fee, and the wallet has to be left
        # able to build a change box; a balance of exactly the payment is not enough.
        required = __mu_to_nanoerg(amount) + DEFAULT_FEE + SAFE_MIN_BOX_VALUE
        available = __confirmed_balance_nanoerg(__get_sender_addr(WALLET_MNEMONIC()))
        check = available > required
        if not check:
            LOGGER(f"Insufficient balance for the wallet. Required: {required}, Available: {available}")
        return check
    except Exception as e:
        LOGGER(f"Error checking wallet balance: {str(e)}")
        return False


def compute_sweep_amount(
    balance_nanoerg: int,
    hot_limit_nanoerg: int,
    min_transfer_nanoerg: int,
    fee_nanoerg: int = DEFAULT_FEE,
    technical_min_nanoerg: int = SAFE_MIN_BOX_VALUE,
) -> Optional[int]:
    """Ergo's nanoERG sweep decision: this ledger's constants, the shared rule.

    The arithmetic lives in ``payment_system.sweeps`` because every payment system with
    a hot wallet wants exactly this decision and only the units differ. What stays here
    are Ergo's own floors as the defaults, and the nanoERG parameter names that the
    callers and tests in this package read.
    """
    return _compute_sweep_amount(
        balance=balance_nanoerg,
        hot_limit=hot_limit_nanoerg,
        min_transfer=min_transfer_nanoerg,
        fee=fee_nanoerg,
        technical_min=technical_min_nanoerg,
    )


def manager():
    """One periodic tick for this contract: pay what is owed, then sweep what is spare.

    Donations first, deliberately. A donation is a debt this node already incurred
    against payments it has been paid; the sweep only moves the node's own funds
    between its own wallets and pays nobody, so it takes what is left rather than
    what it would have taken before the debt was settled.

    One tick per *contract*, not per asset. Once this contract settles in more than
    one asset (#342) a tick dispatched per asset would run N+1 times and pay N+1 fees
    for one job -- and an Ergo transaction carries several assets in one output, so the
    outputs of every asset owed belong in the single transaction built below.
    """
    _pay_accrued_donations()
    _sweep_to_cold_wallet()


def _pay_accrued_donations():
    """Pay every asset's donation debt on this contract, in ONE transaction.

    The order and the bookkeeping are shared (`donations.payout`); what is Ergo's is the
    money -- its fee, its minimum box value, how an amount reads, and how to send.

    One transaction for every asset, because an Ergo output carries them all: a payout
    per asset would pay a fee per asset, and each of those fees is money donated on top
    of the share the operator configured.

    ERG's debt declares the fee, so the fee comes out of the share rather than on top of
    it (#282). A token's declares none, because an Ergo fee is paid in ERG and a debt in
    SigUSD cannot pay it -- so paying a token donation costs this node ERG: the fee, and
    a carrier box for any wallet that is owed only tokens. That is the same asymmetry as
    every other token operation here, and a wallet with no ERG cannot do it at all.
    """
    from src.payment_system.donations import config as donation_config
    from src.payment_system.donations.payout import Debt, pay_accrued_together

    assets = rate.assets()
    debts = [Debt(
        asset=NATIVE_ASSET,
        fee=DEFAULT_FEE,
        minimum_output=SAFE_MIN_BOX_VALUE,
        min_transfer=erg_to_nanoerg(donation_config.min_transfer(LEDGER, NATIVE_ASSET)),
        render=lambda amount: f"{nanoerg_to_erg_str(amount)} ERG",
        to_mu=rate.nanoerg_to_mu,
    )]
    for asset in assets:
        debts.append(Debt(
            asset=asset.token_id,
            # Not this asset's money: see the class' own note.
            fee=0,
            # One base unit is the smallest thing a token output can carry.
            minimum_output=1,
            min_transfer=rate.whole_to_base_units(
                donation_config.min_transfer(LEDGER, asset.token_id), asset,
                what=f"ledgers.{LEDGER}.payments.ASSETS[{asset.symbol}]."
                     "DONATION_MIN_TRANSFER"),
            render=partial(_render_asset, asset=asset),
            to_mu=partial(rate.base_units_to_mu, asset=asset),
        ))

    def send(outputs) -> str:
        """One box per destination, carrying everything that destination is owed."""
        built, carriers = [], 0
        for address, amounts in outputs:
            nanoerg = int(amounts.get(NATIVE_ASSET, 0))
            tokens = [(a, int(v)) for a, v in amounts.items() if a != NATIVE_ASSET]
            if tokens and nanoerg < SAFE_MIN_BOX_VALUE:
                # A wallet owed only tokens still needs a box to receive them in, and
                # that ERG is not part of any debt.
                carriers += SAFE_MIN_BOX_VALUE - nanoerg
                nanoerg = SAFE_MIN_BOX_VALUE
            built.append((address, nanoerg, tokens))
        if carriers:
            LOGGER(
                f"Paying {nanoerg_to_erg_str(carriers)} ERG of carrier boxes so the "
                "token donations have somewhere to land. That ERG is this node's own, "
                "not part of any debt."
            )
        return _send_assets(built, fee_nanoerg=DEFAULT_FEE)

    def can_cover(totals) -> bool:
        wallets = max(1, len(donation_config.pay_wallets(LEDGER)))
        # The ERG going out, the fee, and a change box -- a transaction that leaves
        # nothing spendable behind cannot be built.
        required = int(totals.get(NATIVE_ASSET, 0)) + DEFAULT_FEE + SAFE_MIN_BOX_VALUE
        if any(asset != NATIVE_ASSET for asset in totals):
            # Plus a carrier box per destination. Counted for every wallet rather than
            # for the ones that turn out to need it: over-asking leaves the debt accrued
            # for another tick, under-asking builds a transaction the network refuses.
            required += SAFE_MIN_BOX_VALUE * wallets
        total = __balance_total(address=__get_sender_addr(WALLET_MNEMONIC()))
        held = int(((total or {}).get("confirmed") or {}).get("nanoErgs") or 0)
        if held < required:
            LOGGER(
                f"The donation payout needs {nanoerg_to_erg_str(required)} ERG for the "
                f"amounts, the fee and the boxes; the wallet holds "
                f"{nanoerg_to_erg_str(held)} ERG."
            )
            return False
        for asset_id, owed in totals.items():
            if asset_id == NATIVE_ASSET:
                continue
            if _token_balance(total, asset_id) < int(owed):
                LOGGER(
                    f"The donation payout owes {owed} base units of {asset_id[:12]} and "
                    "the wallet does not hold them."
                )
                return False
        return True

    pay_accrued_together(
        ledger=LEDGER,
        contract_hash=CONTRACT_HASH,
        debts=debts,
        send=send,
        valid_address=is_valid_ergo_address,
        simulate=SIMULATE_PAYMENTS(),
        available=can_cover,
        # The same lock a deposit takes. Without it a donation and a payment can pick
        # the same input box and one of them becomes a double spend.
        lock=payment_lock,
    )


def _render_asset(amount: int, asset) -> str:
    """A base-unit amount of one asset as a person reads it."""
    return f"{rate.base_units_to_str(amount, asset)} {asset.symbol}"


def _send_assets(outputs, fee_nanoerg: int) -> str:
    """One transaction, one output per destination, each carrying ERG and any tokens.

    ``outputs`` is ``[(address, nanoerg, [(token_id, base_units), ...]), ...]``.

    This exists because an Ergo transaction carries several assets in one output, and
    both jobs on this contract's tick want exactly that: the sweep moves every asset
    over its limit to the cold wallet, and the donation payout pays every debt it owes,
    each in **one** transaction rather than one per asset. Going through ergpy's
    ``simple_send`` instead would mean one ERG-only transaction per asset, paying a fee
    every time for what is one piece of work.

    Whatever the inputs hold beyond what these outputs name comes back as change to this
    wallet, which AppKit builds -- so an asset this transaction is not moving is kept,
    never swept by accident.
    """
    _, _, jpype, org_appkit = _ergo_runtime()
    ergo = __init_ergo()
    sender_address = __get_sender_addr(WALLET_MNEMONIC())

    total_nanoerg = sum(int(value) for _address, value, _tokens in outputs)
    token_ids, token_amounts = [], []
    for _address, _value, tokens in outputs:
        for token_id, units in tokens:
            token_ids.append(token_id)
            token_amounts.append(int(units))

    # `amount_list` is read by ergpy in whole ERG.
    input_utxo = ergo.getInputBoxCovering(
        amount_list=[__nanoerg_to_erg(total_nanoerg + fee_nanoerg)],
        sender_address=sender_address,
        tokenList=[token_ids] if token_ids else None,
        amount_tokens=[token_amounts] if token_ids else None,
    )
    if not input_utxo:
        raise Exception("No UTXO found to build the transaction from.")

    ergo_token = _ergo_token_class(jpype, org_appkit) if token_ids else None
    out_boxes = []
    for address, value, tokens in outputs:
        builder = ergo._ctx.newTxBuilder().outBoxBuilder().value(int(value))
        if tokens:
            builder = builder.tokens([
                ergo_token(token_id, jpype.JLong(int(units))) for token_id, units in tokens
            ])
        # Through this package's own helpers rather than AppKit's address classes: the
        # payment path already exchanges propositionBytes and builds contracts from
        # them, so a destination written as a base58 address takes the same route.
        out_boxes.append(builder.contract(
            ergo_contract_from_proposition_bytes(proposition_bytes_from_address(address))
        ).build())

    unsigned_tx = ergo.buildUnsignedTransaction(
        input_box=input_utxo,
        outBox=out_boxes,
        fee=fee_nanoerg / 10**9,
        sender_address=sender_address,
    )
    w_mnemonic = ergo.getMnemonic(wallet_mnemonic=WALLET_MNEMONIC(), mnemonic_password=None)[0]
    signed_tx = ergo.signTransaction(unsigned_tx, w_mnemonic, prover_index=0)
    return str(ergo.txId(signed_tx))


def _asset_sweep_limits(asset) -> Tuple[int, int]:
    """``(hot limit, minimum transfer)`` for one asset, in its base units.

    Per asset, in whole units of it, the same decimal-string shape as ERG's. Absent
    keys mean zero -- which is a hot limit of nothing, so everything above the minimum
    transfer is swept. That is the same reading ERG's keys get.
    """
    entry = _asset_config_entry(asset.token_id)
    return (
        rate.whole_to_base_units(
            entry.get("HOT_WALLET_LIMITS"), asset,
            what=f"{rate.ASSETS_KEY}[{asset.symbol}].HOT_WALLET_LIMITS"),
        rate.whole_to_base_units(
            entry.get("COLD_WALLET_MIN_TRANSFER"), asset,
            what=f"{rate.ASSETS_KEY}[{asset.symbol}].COLD_WALLET_MIN_TRANSFER"),
    )


def _asset_config_entry(token_id: str) -> dict:
    """The operator's ``ASSETS:`` entry for one token id, or an empty block."""
    for entry in env_manager.get(rate.ASSETS_KEY) or []:
        if isinstance(entry, dict) and str(entry.get("TOKEN_ID") or "").strip().lower() == token_id:
            return entry
    return {}


def _sweep_to_cold_wallet():
    """Sweep every asset over its own limits to the cold wallet, in ONE transaction.

    One transaction, not one per asset: an Ergo output carries several assets at once,
    and the cold wallet is a single Ergo address that receives whatever is sent to it
    (which is why ``COLD_WALLET`` is not per asset while the limits are).

    A token sweep still costs ERG -- the fee, and the box the tokens travel in -- so a
    wallet without it cannot sweep at all, however many tokens it holds. That is the
    same asymmetry as paying: being *paid* in a token needs no ERG, moving one does.
    """
    LOGGER("Exec ergo interface manager (single-wallet cold sweep).")
    try:
        cold_wallet = COLD_WALLET()
        if not cold_wallet:
            LOGGER("No cold wallet configured; skipping sweep.")
            return

        wallet_addr = __get_sender_addr(WALLET_MNEMONIC())
        total = __balance_total(address=wallet_addr)
        balance_nano = int(((total or {}).get("confirmed") or {}).get("nanoErgs") or 0)
        hot_limit_nano = _hot_wallet_limit_nanoerg()
        min_transfer_nano = _cold_wallet_min_transfer_nanoerg()

        sweep_nano = compute_sweep_amount(
            balance_nanoerg=balance_nano,
            hot_limit_nanoerg=hot_limit_nano,
            min_transfer_nanoerg=min_transfer_nano,
            fee_nanoerg=DEFAULT_FEE,
        )

        token_sweeps = []
        for asset in rate.assets():
            hot_limit, min_transfer = _asset_sweep_limits(asset)
            held = _token_balance(total, asset.token_id)
            # `fee=0` and `technical_min=1`: the fee is not this asset's money, and the
            # smallest token output is one base unit. The ERG those need is checked
            # once, below, for the whole transaction.
            amount = _compute_sweep_amount(
                balance=held, hot_limit=hot_limit, min_transfer=min_transfer,
                fee=0, technical_min=1,
            )
            if amount:
                token_sweeps.append((asset, amount))

        if sweep_nano is None and not token_sweeps:
            LOGGER(
                f"Nothing to sweep. balance={balance_nano} hot_limit={hot_limit_nano} "
                f"min_transfer={min_transfer_nano} fee={DEFAULT_FEE} (all nanoERG), and "
                f"no configured asset is over its limit."
            )
            return

        # The box the sweep travels in. When only tokens are moving there is no ERG
        # excess to carry them, so the technical minimum comes out of the hot wallet --
        # the unavoidable ERG cost of moving a token (#342 4.4).
        box_value = sweep_nano if sweep_nano is not None else SAFE_MIN_BOX_VALUE
        # Plus a change box: a transaction that leaves nothing spendable cannot be built.
        required = box_value + DEFAULT_FEE + (0 if sweep_nano is not None else SAFE_MIN_BOX_VALUE)
        if balance_nano < required:
            LOGGER(
                f"Not sweeping: it needs {nanoerg_to_erg_str(required)} ERG for the fee "
                f"and the output box and the wallet holds "
                f"{nanoerg_to_erg_str(balance_nano)} ERG. "
                f"{len(token_sweeps)} asset(s) stay in the hot wallet."
            )
            return

        moving = [f"{nanoerg_to_erg_str(box_value)} ERG"] + [
            f"{rate.base_units_to_str(amount, asset)} {asset.symbol}"
            for asset, amount in token_sweeps
        ]
        LOGGER(
            f"Sweeping {', '.join(moving)} from the wallet to cold wallet {cold_wallet} "
            f"(fee {nanoerg_to_erg_str(DEFAULT_FEE)} ERG)."
        )
        tx = _send_assets(
            [(cold_wallet, box_value,
              [(asset.token_id, amount) for asset, amount in token_sweeps])],
            fee_nanoerg=DEFAULT_FEE,
        )
        LOGGER(f"Cold sweep tx -> {tx}")
    except Exception as e:
        LOGGER(f"Exception on cold sweep -> {str(e)}")


def _ergo_token_class(jpype, org_appkit):
    """``ErgoToken``, from whichever package this AppKit build puts it in.

    It moved from ``org.ergoplatform.appkit`` to ``org.ergoplatform.sdk``; the
    reputation system already carries this fallback and the payment path needs the same
    one, or a token payment fails at signing time on one of the two builds.
    """
    try:
        return org_appkit.ErgoToken
    except AttributeError:
        return jpype.JPackage("org").ergoplatform.sdk.ErgoToken


# Function to process the payment, generating a transaction with the token in register R4
def process_payment(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> celaut_pb2.Contract:
    """Pay ``amount`` MU in ERG, the native unit of this ledger."""
    return _settle(amount=amount, deposit_token=deposit_token, ledger=ledger,
                   script=script, asset=None)


def _settle(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger,
            script: bytes, asset) -> celaut_pb2.Contract:
    """One payment, in ERG when ``asset`` is ``None`` and in that token otherwise.

    One implementation rather than two, because everything that is *Ergo* here is shared
    -- the wallet, the lock, the R4 deposit token, submitting, and waiting for two
    confirmations -- and only the shape of the output box differs:

    * In ERG the box **is** the payment: its value is the converted amount.
    * In a token the box *carries* the payment: its value is the technical minimum a box
      needs to exist (``SAFE_MIN_BOX_VALUE`` nanoERG, supplied by the payer along with
      the fee, see #342 4.4) and the money is in its token list.

    Anything else the input boxes happen to carry is returned to this wallet as change,
    which AppKit builds. It is never sent to the payee: an asset nobody asked for is not
    a payment, and forwarding one would mean paying for a transfer nobody requested.
    """
    with payment_lock:
        if asset is None:
            box_value = __mu_to_nanoerg(amount)
            LOGGER(f"Process ergo platform payment for token {deposit_token} of {box_value} nanoERG")

            # Ergo rejects an output below the technical minimum box value, so a payment
            # worth less than that cannot be settled on-chain at all. Fail loudly here
            # instead of building a transaction the network will refuse.
            if box_value < SAFE_MIN_BOX_VALUE:
                raise Exception(
                    f"Payment of {nanoerg_to_erg_str(box_value)} ERG is below Ergo's minimum box "
                    f"value ({nanoerg_to_erg_str(SAFE_MIN_BOX_VALUE)} ERG). Nothing can be "
                    "settled for that amount; see deposits.MAX_FEE_OVERHEAD in the config."
                )
            token_units = 0
        else:
            token_units = rate.mu_to_base_units(amount, asset)
            LOGGER(
                f"Process ergo platform payment for token {deposit_token} of "
                f"{rate.base_units_to_str(token_units, asset)} {asset.symbol}"
            )
            # The smallest thing a token output can carry is one base unit. Below that
            # there is nothing to put in the box, and a box with an empty token list is
            # a payment of zero dressed as a payment.
            if token_units < 1:
                raise Exception(
                    f"Payment of {amount} MU is less than one base unit of "
                    f"{asset.symbol}, so there is nothing to settle; see "
                    "deposits.MAX_FEE_OVERHEAD in the config."
                )
            # The carrier value, not the payment: it is ERG this node supplies so the
            # token has a box to travel in.
            box_value = SAFE_MIN_BOX_VALUE

        try:
            _, _, jpype, org_appkit = _ergo_runtime()
            ergo = __init_ergo()
            sender_address = __get_sender_addr(WALLET_MNEMONIC())

            if asset is None:
                # NOTE: `box_value` is nanoERG and ergpy reads `amount_list` in whole
                # ERG (`Parameters.OneErg * sum(...)`), so this asks for 1e9 times the
                # payment. `getCoveringBoxesFor` cannot cover that and hands back the
                # boxes it did load, which do cover the real amount -- so it works, by
                # accident. Left as it is rather than corrected blind: it is the
                # settled ERG path and there is no Ergo node here to try the fix
                # against. The token branch below passes the figure honestly.
                input_utxo = ergo.getInputBoxCovering(
                    amount_list=[box_value],
                    sender_address=sender_address
                )
            else:
                # The inputs have to cover the token as well as the ERG, or the built
                # transaction is short of the very thing it is paying in. `amount_list`
                # is read by ergpy in whole ERG (`Parameters.OneErg * sum(...)`), and
                # what this needs is the carrier box plus the fee.
                input_utxo = ergo.getInputBoxCovering(
                    amount_list=[__nanoerg_to_erg(box_value + DEFAULT_FEE)],
                    sender_address=sender_address,
                    tokenList=[[asset.token_id]],
                    amount_tokens=[[token_units]],
                )
            if not input_utxo:
                raise Exception("No UTXO found for the contract address with the required token.")

            # ``script`` is the raw ErgoTree/propositionBytes; convert to an ErgoContract only
            # here, at the AppKit boundary. No textual-address decoding.
            builder = ergo._ctx.newTxBuilder() \
                        .outBoxBuilder() \
                        .value(box_value)
            if asset is not None:
                ergo_token = _ergo_token_class(jpype, org_appkit)
                builder = builder.tokens([ergo_token(asset.token_id, jpype.JLong(token_units))])
            out_box = builder \
                        .registers([
                            org_appkit.ErgoValue.of(jpype.JString(deposit_token).getBytes("utf-8"))
                        ]) \
                        .contract(ergo_contract_from_proposition_bytes(script)) \
                        .build()

            unsigned_tx = ergo.buildUnsignedTransaction(
                input_box=input_utxo,
                outBox=[out_box],
                fee=DEFAULT_FEE / 10**9,
                sender_address=sender_address
            )

            w_mnemonic = ergo.getMnemonic(wallet_mnemonic=WALLET_MNEMONIC(), mnemonic_password=None)[0]
            signed_tx = ergo.signTransaction(unsigned_tx, w_mnemonic, prover_index=0)

            try:
                tx_id = ergo.txId(signed_tx)
                LOGGER(
                    "Transaction submitted: "
                    f"https://sigmaspace.io/en/transaction/{tx_id} "
                    f"for token {deposit_token}"
                )
                reporter = _transaction_url_reporter.get()
                if reporter:
                    reporter(f"https://sigmaspace.io/en/transaction/{tx_id}")
                id_reporter = _transaction_id_reporter.get()
                if id_reporter:
                    id_reporter(tx_id)
            except Exception as e:
                if "Double spending attempt" in str(e):
                    raise DoubleSpendingAttempt(LEDGER)
                else:
                    raise e

            for _ in range(0, WAIT_TX_TIME):
                sleep(WAT_TX_SLEEP_TIME)
                response = requests.get(f"{ergo.get_api_url()}/api/v1/transactions/{tx_id}")
                if response.status_code != 200:
                    if response.status_code != 404:
                        LOGGER(f"{ergo.get_api_url()} tx {tx_id} check failed: {response.status_code}")
                    continue

                obj = response.json()
                if obj["numConfirmations"] > 1:
                    LOGGER(f"Tx {tx_id} verified.")
                    contract = celaut_pb2.Contract(ledger=ledger)
                    # Which asset was paid, so the peer files the credit against the
                    # method it advertised rather than against this contract's default.
                    set_token_id(contract, NATIVE_ASSET if asset is None else asset.token_id)
                    set_script(contract, script)
                    set_contract_type(contract, CONTRACT.encode("utf-8"))
                    return contract

            raise Exception(f"Can't verify the tx {tx_id}")

        except Exception as e:
            raise e


# Validate the payment by checking for an unspent box with the token in register R4 at the wallet.
def payment_process_validator(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> bool:
    """Prove an incoming ERG payment."""
    return _validate(amount=amount, token=token, ledger=ledger, script=script, asset=None)


def _box_token_amount(box_dict: dict, token_id: str) -> int:
    """How much of ``token_id`` a box carries, scanning its whole asset list.

    The **whole** list, never ``assets[0]``. The reputation reader can take the first
    asset of a box because this node built that box and put exactly one token in it; a
    payment box is built by the *payer*, so its asset order is the payer's choice and
    reading position zero would reject an honest payment that happened to list another
    token first -- with the money already on-chain.

    A box carrying the same id twice is not something Ergo produces, but summing rather
    than taking the first match costs nothing and cannot under-count.
    """
    total = 0
    for entry in box_dict.get("assets") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("tokenId") or "").lower() == token_id:
            try:
                total += int(entry.get("amount") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _validate(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes,
              asset) -> bool:
    """Prove one incoming payment: ERG when ``asset`` is ``None``, that token otherwise.

    Being paid in a token needs no ERG in this wallet at all -- the payer supplies both
    the fee and the box the token travels in (#342 4.4) -- so nothing here reads this
    node's own balance.
    """
    try:
        assert LEDGER in ledger.tags, "Ledger does not match"

        # ``script`` is the raw propositionBytes; derive the readable address only here.
        from src.payment_system.contracts.ergo.ergo_tree import address_from_proposition_bytes
        address = str(address_from_proposition_bytes(script).toString())
        assert address == get_wallet_address(), "Contract address does not match the node wallet"

        ergo = __init_ergo()
        explorer_api = ergo.get_api_url()
        url = f"{explorer_api}/api/v1/boxes/unspent/unconfirmed/byAddress/{address}"
        response = requests.get(url)
        if response.status_code != 200:
            LOGGER(f"Error fetching UTXOs: {response.status_code} - {response.text}")
            return False

        utxos = response.json()
        expected = (
            __mu_to_nanoerg(amount) if asset is None
            else rate.mu_to_base_units(amount, asset)
        )
        for box_dict in utxos:
            if "additionalRegisters" in box_dict and "R4" in box_dict["additionalRegisters"]:
                r4_value = box_dict["additionalRegisters"]["R4"]["renderedValue"]
                decoded_r4 = bytes.fromhex(r4_value).decode("utf-8")
                if decoded_r4 == token:
                    # At least, not exactly: the payer converts our MU figure from
                    # its own scale and has to round down to a whole MU of ours,
                    # so a correct payment routinely carries a few nanoERG more
                    # than the credit it asks for. Demanding equality rejected
                    # those with the money already on-chain. More than asked for
                    # is never a problem -- the credit is what `expected` covers,
                    # and the excess stays in this wallet rather than being returned:
                    # sending it back would mean building and paying for a
                    # transaction nobody asked for.
                    if asset is None:
                        paid = box_dict.get("value")
                        rendered = f"{paid} nanoERG"
                    else:
                        paid = _box_token_amount(box_dict, asset.token_id)
                        rendered = f"{paid} base units of {asset.symbol}"
                    if paid is not None and paid >= expected:
                        return True
                    LOGGER(
                        f"Insufficient amount for token {token}. Was {rendered}, "
                        f"expected at least {expected}"
                    )
                    return False

        LOGGER(f"Token {token} not found in R4.")
        return False

    except Exception as e:
        LOGGER(f"Error validating payment process: {str(e)}")
        return False


def _token_settlement_floors_mu(asset) -> Tuple[int, int]:
    """``(fee, smallest payable output)`` for a token method, in MU -- two assets in one pair.

    The promise of this function is "both figures in MU", and for a token that is the
    only scale that can hold both: the fee is paid in ERG and converts through
    ``MU_PER_NANOERG``, while the smallest output is **one base unit of the token** and
    converts through the token's own rate. `deposits.py` consumes MU and needs no more
    than that (#342 5.4).

    The fee counted here is the network fee *plus* the carrier box, because both are ERG
    the payer parts with beyond the amount credited -- which is exactly what a fee
    overhead is measuring. Charging only the network fee would size deposits as if the
    carrier were free and let a token payment spend more on overhead than the operator's
    ``MAX_FEE_OVERHEAD`` allows.
    """
    return (
        rate.nanoerg_to_mu(DEFAULT_FEE + SAFE_MIN_BOX_VALUE),
        rate.base_units_to_mu(1, asset),
    )


def _token_balance(total: Optional[dict], token_id: str) -> int:
    """Confirmed balance of one token, in base units, out of a balance payload.

    Takes the payload rather than an address so one read answers for every asset: the
    explorer reports the ERG and the tokens of a wallet in the same response, and
    asking twice would both double the calls on the payment path and let the two halves
    of one decision see two different balances.
    """
    if not total:
        return 0
    return _box_token_amount(
        {"assets": (total.get("confirmed") or {}).get("tokens") or []}, token_id
    )


def _token_check_sender_balance(amount: int, asset) -> bool:
    """Can this wallet pay ``amount`` MU in ``asset``? Two assets have to answer yes.

    A token transaction still pays its fee in ERG and still needs
    ``SAFE_MIN_BOX_VALUE`` nanoERG to carry the token in the output, plus enough left to
    build a change box. So a wallet full of the token and empty of ERG cannot pay --
    which is the asymmetry of #342 4.4: such a node can be *paid* in the token
    without ever holding ERG, but it cannot pay and cannot sweep.

    Both shortfalls are named in the log, because "insufficient balance" on a wallet
    visibly holding the token is a message an operator cannot act on.
    """
    try:
        units = rate.mu_to_base_units(amount, asset)
        # The carrier box the token travels in, the fee, and enough left to build a
        # change box: a transaction that leaves nothing spendable behind cannot be built.
        required_nanoerg = DEFAULT_FEE + SAFE_MIN_BOX_VALUE + SAFE_MIN_BOX_VALUE
        # One read for both figures, so the two halves of this decision cannot disagree.
        total = __balance_total(address=__get_sender_addr(WALLET_MNEMONIC()))
        available_nanoerg = int(((total or {}).get("confirmed") or {}).get("nanoErgs") or 0)
        available_units = _token_balance(total, asset.token_id)

        missing = []
        if available_units < units:
            missing.append(
                f"{asset.symbol}: required {rate.base_units_to_str(units, asset)}, "
                f"available {rate.base_units_to_str(available_units, asset)}"
            )
        if available_nanoerg <= required_nanoerg:
            missing.append(
                f"ERG for the fee and the carrier box: required "
                f"{nanoerg_to_erg_str(required_nanoerg)}, available "
                f"{nanoerg_to_erg_str(available_nanoerg)}"
            )
        if missing:
            LOGGER(f"Insufficient balance for the wallet. {'; '.join(missing)}.")
            return False
        return True
    except Exception as e:
        LOGGER(f"Error checking wallet balance: {str(e)}")
        return False


def methods():
    """This contract's payment methods: ERG, plus one per configured asset.

    ``1 + N`` from one module, one wallet and one lock. They share everything that is
    Ergo's -- the wallet, ``payment_lock``, the AppKit session, the explorer client and
    the ErgoTree helpers -- because a token on Ergo is not another contract: it is the
    same P2PK script paid in different money (#342 4.1). Only the calls whose answer
    depends on *which asset* are bound per method; everything else, including the
    per-contract ``init`` and ``manager`` ticks, is forwarded to this module unchanged.

    ERG comes first and the assets follow in the operator's declared order, because
    that order is the payer's preference: the payment walk tries one method and falls
    through to the next, so a node out of SigUSD but holding ERG pays in ERG with no
    policy and no new setting -- and that has to be reproducible rather than
    set-ordered.
    """
    from sys import modules

    from src.payment_system.contracts.registry import PaymentMethod

    # This module *is* the contract: a method that overrides nothing has to forward
    # every call to it, so what a `PaymentMethod` wraps here is the module itself.
    _this_module = modules[__name__]
    built = [PaymentMethod(_this_module, NATIVE_ASSET, unit_name=rate.UNIT_NAME)]
    for asset in rate.assets():
        built.append(PaymentMethod(_this_module, asset.token_id,
                                   symbol=asset.symbol, unit_name=asset.unit_name,
                                   calls={
            "mu_per_unit": partial(rate.mu_per_whole_unit, asset),
            "settlement_floors_mu": partial(_token_settlement_floors_mu, asset),
            "mu_to_native": partial(rate.mu_to_base_units_exact, asset=asset),
            "check_sender_balance": partial(_token_check_sender_balance, asset=asset),
            "process_payment": partial(_settle, asset=asset),
            "payment_process_validator": partial(_validate, asset=asset),
        }))
    return built
