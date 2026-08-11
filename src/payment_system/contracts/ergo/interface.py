from src.reputation_system.envs import ergo_ledger
from typing import Optional, Tuple
from protos import celaut_pb2
import requests
from hashlib import sha3_256
from src.database import sql_connection
from src.payment_system.exceptions import DoubleSpendingAttempt
from src.utils.logger import LOGGER
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_address, set_script, set_token_id, set_contract_type
from src.utils.ergo_units import erg_to_nanoerg, nanoerg_to_erg_str
# This ledger's MU rate and its conversions. A separate, light module on purpose: it is
# also what `monetary.display_unit` resolves ERG through, and that runs on log lines, so
# it must not pull in everything below.
from src.payment_system.contracts.ergo import rate
from src.utils.ergo_tree import (
    ergo_contract_from_proposition_bytes,
    proposition_bytes_from_address,
)
from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module
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

# The node controls exactly ONE wallet. Clients pay directly to its P2PK address; excess is
# swept to the cold wallet (a public address, never a mnemonic in Nodo).
WALLET_MNEMONIC = lambda: env_manager.get("ledgers.ergo.WALLET_MNEMONIC")
ERGO_NODE_URL = lambda: env_manager.get("ledgers.ergo.NODE_URL")
COLD_WALLET = lambda: env_manager.get("ledgers.ergo.payments.COLD_WALLET") or ""
ERGO_DONATION_WALLET = lambda: env_manager.get("ledgers.ergo.payments.DONATION_WALLET") or ""


def _clamp(value: float, maximum: float, minimum: float) -> float:
    return max(minimum, min(value, maximum))


def DONATION_PERCENTAGE() -> float:
    raw = env_manager.get("ledgers.ergo.payments.DONATION_PERCENTAGE") or "0"
    return _clamp(float(raw), 1.0, 0.0)


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


@contextmanager
def transaction_url_reporting(reporter):
    """Temporarily report a submitted Ergo transaction URL to the caller."""
    token = _transaction_url_reporter.set(reporter)
    try:
        yield
    finally:
        _transaction_url_reporter.reset(token)


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


def get_balance() -> Tuple[str, float]:
    """Return (address, confirmed balance in ERG) for the single wallet."""
    addr = __get_sender_addr(WALLET_MNEMONIC())
    return str(addr.toString()), __nanoerg_to_erg(__confirmed_balance_nanoerg(addr))


def init():
    """Advertise the single payment contract: raw wallet propositionBytes as the script."""
    proposition_bytes = get_wallet_proposition_bytes()
    sql = sql_connection.SQLConnection()
    contract = celaut_pb2.Contract(ledger=ergo_ledger)
    set_token_id(contract, "ERG")
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
    """
    Pure nanoERG sweep decision. Returns the integer amount to move to the cold wallet, or
    ``None`` when nothing should be swept.

    excess = balance - hot_limit - fee. Sweep only when the excess is at least the cold-wallet
    minimum transfer AND a valid Ergo output (>= technical minimum). The hot limit, the fee,
    and the technical minimum are always retained. All arithmetic is integer nanoERG.
    """
    excess = balance_nanoerg - hot_limit_nanoerg - fee_nanoerg
    if excess < min_transfer_nanoerg:
        return None
    if excess < technical_min_nanoerg:
        return None
    return excess


def manager():
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

        # Optional donation split (percentage of the swept amount), integer nanoERG.
        donation_wallet = ERGO_DONATION_WALLET()
        donation_pct = DONATION_PERCENTAGE()
        donation_nano = int(sweep_nano * donation_pct)

        receiver_addresses = [cold_wallet]
        amounts_nano = [sweep_nano]
        if donation_wallet and donation_nano >= SAFE_MIN_BOX_VALUE:
            amounts_nano = [sweep_nano - donation_nano, donation_nano]
            receiver_addresses = [cold_wallet, donation_wallet]
            LOGGER(f"Donation split: {nanoerg_to_erg_str(donation_nano)} ERG -> donation wallet.")

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
                    set_token_id(contract, "ERG")
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
        from src.utils.ergo_tree import address_from_proposition_bytes
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
                    if "value" in box_dict and box_dict["value"] == expected:
                        return True
                    LOGGER(f"Incorrect amount for token {token}. Was {box_dict.get('value')} expected {expected}")
                    return False

        LOGGER(f"Token {token} not found in R4.")
        return False

    except Exception as e:
        LOGGER(f"Error validating payment process: {str(e)}")
        return False
