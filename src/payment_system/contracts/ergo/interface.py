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
    """Advertise the single payment contract: raw wallet propositionBytes as the script."""
    proposition_bytes = get_wallet_proposition_bytes()
    sql = sql_connection.SQLConnection()
    contract = celaut_pb2.Contract(ledger=ergo_ledger)
    set_token_id(contract, NATIVE_ASSET)
    # Canonical value: raw ErgoTree/propositionBytes of the wallet's P2PK payment boxes.
    set_script(contract, proposition_bytes)
    # Stable type identity for cross-node matching (its sha3 == CONTRACT_HASH).
    set_contract_type(contract, CONTRACT.encode("utf-8"))
    # Derived address for local display/indexing only; never the source of truth.
    set_address(contract, get_wallet_address())
    sql.add_contract(contract=contract)


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
    """Pay the donation debt accrued on this contract, if a transaction is worth making.

    The order and the bookkeeping are shared (`donations.payout`); what is Ergo's is the
    money -- its fee, its minimum box value, how a nanoERG figure reads, and how to send.
    """
    from src.payment_system.donations import config as donation_config
    from src.payment_system.donations.payout import pay_accrued

    def send(outputs, fee_nanoerg: int) -> str:
        _, simple_send, _, _ = _ergo_runtime()
        return str(simple_send(
            ergo=__init_ergo(),
            # simple_send expects ERG amounts.
            amount=[__nanoerg_to_erg(amount) for _, amount in outputs],
            receiver_addresses=[address for address, _ in outputs],
            wallet_mnemonic=WALLET_MNEMONIC(),
            fee=__nanoerg_to_erg(fee_nanoerg),
        ))

    def can_cover(total_nanoerg: int) -> bool:
        # Plus a change box: a transaction that leaves nothing spendable behind cannot
        # be built at all.
        required = total_nanoerg + SAFE_MIN_BOX_VALUE
        return __confirmed_balance_nanoerg(__get_sender_addr(WALLET_MNEMONIC())) >= required

    pay_accrued(
        ledger=LEDGER,
        contract_hash=CONTRACT_HASH,
        asset=NATIVE_ASSET,
        fee=DEFAULT_FEE,
        minimum_output=SAFE_MIN_BOX_VALUE,
        min_transfer=erg_to_nanoerg(donation_config.min_transfer(LEDGER, NATIVE_ASSET)),
        send=send,
        render=lambda amount: f"{nanoerg_to_erg_str(amount)} ERG",
        to_mu=rate.nanoerg_to_mu,
        valid_address=is_valid_ergo_address,
        simulate=SIMULATE_PAYMENTS(),
        available=can_cover,
        # The same lock a deposit takes. Without it a donation and a payment can pick
        # the same input box and one of them becomes a double spend.
        lock=payment_lock,
    )


def _sweep_to_cold_wallet():
    """Sweep excess from the single wallet to the cold wallet when both thresholds are met."""
    LOGGER("Exec ergo interface manager (single-wallet cold sweep).")
    try:
        cold_wallet = COLD_WALLET()
        if not cold_wallet:
            LOGGER("No cold wallet configured; skipping sweep.")
            return

        _, simple_send, _, _ = _ergo_runtime()
        wallet_addr = __get_sender_addr(WALLET_MNEMONIC())
        balance_nano = __confirmed_balance_nanoerg(wallet_addr)
        hot_limit_nano = _hot_wallet_limit_nanoerg()
        min_transfer_nano = _cold_wallet_min_transfer_nanoerg()

        sweep_nano = compute_sweep_amount(
            balance_nanoerg=balance_nano,
            hot_limit_nanoerg=hot_limit_nano,
            min_transfer_nanoerg=min_transfer_nano,
            fee_nanoerg=DEFAULT_FEE,
        )
        if sweep_nano is None:
            LOGGER(
                f"Nothing to sweep. balance={balance_nano} hot_limit={hot_limit_nano} "
                f"min_transfer={min_transfer_nano} fee={DEFAULT_FEE} (all nanoERG)."
            )
            return

        receiver_addresses = [cold_wallet]
        amounts_nano = [sweep_nano]

        LOGGER(
            f"Sweeping {nanoerg_to_erg_str(sweep_nano)} ERG from the wallet to cold wallet "
            f"{cold_wallet} (fee {nanoerg_to_erg_str(DEFAULT_FEE)} ERG)."
        )
        # simple_send expects ERG amounts.
        tx = simple_send(
            ergo=__init_ergo(),
            amount=[__nanoerg_to_erg(a) for a in amounts_nano],
            receiver_addresses=receiver_addresses,
            wallet_mnemonic=WALLET_MNEMONIC(),
            fee=__nanoerg_to_erg(DEFAULT_FEE),
        )
        LOGGER(f"Cold sweep tx -> {tx}")
    except Exception as e:
        LOGGER(f"Exception on cold sweep -> {str(e)}")


# Function to process the payment, generating a transaction with the token in register R4
def process_payment(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> celaut_pb2.Contract:
    with payment_lock:
        amount = __mu_to_nanoerg(amount)
        LOGGER(f"Process ergo platform payment for token {deposit_token} of {amount} nanoERG")

        # Ergo rejects an output below the technical minimum box value, so a payment
        # worth less than that cannot be settled on-chain at all. Fail loudly here
        # instead of building a transaction the network will refuse.
        if amount < SAFE_MIN_BOX_VALUE:
            raise Exception(
                f"Payment of {nanoerg_to_erg_str(amount)} ERG is below Ergo's minimum box "
                f"value ({nanoerg_to_erg_str(SAFE_MIN_BOX_VALUE)} ERG). Nothing can be "
                "settled for that amount; see deposits.MAX_FEE_OVERHEAD in the config."
            )

        try:
            _, _, jpype, org_appkit = _ergo_runtime()
            ergo = __init_ergo()
            sender_address = __get_sender_addr(WALLET_MNEMONIC())

            input_utxo = ergo.getInputBoxCovering(
                amount_list=[amount],
                sender_address=sender_address
            )
            if not input_utxo:
                raise Exception("No UTXO found for the contract address with the required token.")

            # ``script`` is the raw ErgoTree/propositionBytes; convert to an ErgoContract only
            # here, at the AppKit boundary. No textual-address decoding.
            out_box = ergo._ctx.newTxBuilder() \
                        .outBoxBuilder() \
                        .value(amount) \
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
                    set_token_id(contract, NATIVE_ASSET)
                    set_script(contract, script)
                    set_contract_type(contract, CONTRACT.encode("utf-8"))
                    return contract

            raise Exception(f"Can't verify the tx {tx_id}")

        except Exception as e:
            raise e


# Validate the payment by checking for an unspent box with the token in register R4 at the wallet.
def payment_process_validator(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger, script: bytes) -> bool:
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
        expected = __mu_to_nanoerg(amount)
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
                    # is never a problem -- the credit is what `expected` covers.
                    if "value" in box_dict and box_dict["value"] >= expected:
                        return True
                    LOGGER(f"Insufficient amount for token {token}. Was {box_dict.get('value')} expected at least {expected}")
                    return False

        LOGGER(f"Token {token} not found in R4.")
        return False

    except Exception as e:
        LOGGER(f"Error validating payment process: {str(e)}")
        return False
