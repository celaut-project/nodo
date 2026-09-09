"""The Bitcoin payment contract: get paid in BTC, and pay a peer in BTC.

Bitcoin shares the *least* mechanism with Ergo of any chain worth adding, which is why
it is the one that proves the registry is real. It has no register to write a deposit
token into, no fixed fee, no minimum box value, no two-minute finality and no JVM.
Anything that survived being made to work for both is genuinely ledger-agnostic;
anything that broke was Ergo-shaped and pretending otherwise (#340 §1).

**How a deposit is tied to a payment: a static address plus `OP_RETURN`.** The literal
translation of Ergo's register `R4`. The advertised ``script`` xattr stays one static
`scriptPubKey`, `payment_process_validator`'s signature is unchanged, and
`GenerateDepositToken` keeps returning just a token. It costs ~43 extra vB per payment
and it reuses one address -- the same privacy posture Ergo already has here.

The alternative was one derived address per deposit, with an xpub advertised and the
index derived from the token by both sides. Better privacy and cheaper transactions, but
it publishes an xpub to every peer that reads `GetPeerInfo`, which exposes that
account's whole receiving history to anyone on the network. Not for a first version
(#340 §4.1 option B); written down here so the next person does not re-litigate it.

**The payer waits for confirmations, exactly as it does on Ergo.** No new deposit-token
state and no change to the pending/payed/rejected machine: `process_payment` polls until
`MIN_CONFIRMATIONS`, and only then does the orchestrator call `Payable`. The receiver
therefore validates against a transaction that is already final and answers in one call.
That also disposes of RBF for free -- a confirmed transaction cannot be replaced -- so
the only residual risk is a shallow reorg, which is what `MIN_CONFIRMATIONS` is for.

What is genuinely different is only *how long* the wait is, and that breaks two
Ergo-shaped constants rather than the state machine: this contract declares its own
`DEPOSIT_TOKEN_TTL`, and `needs_unspent_proof = False` keeps it out of a sweep pause it
does not need (its proof is a confirmed transaction, not an unspent output).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from hashlib import sha3_256
from threading import Lock
from time import sleep
from typing import Optional, Tuple

from protos import celaut_pb2
from src.database import sql_connection
from src.payment_system.contracts.bitcoin import rate
from src.payment_system.contracts.bitcoin.backend import BackendUnavailable, backend
from src.payment_system.contracts.bitcoin.backend import configuration_reason
from src.payment_system.sweeps import compute_sweep_amount
from src.utils.bitcoin_units import (
    P2WPKH_DUST_SAT,
    address_from_script_pubkey,
    btc_to_satoshi,
    is_valid_bitcoin_address,
    satoshi_to_btc_str,
    script_pubkey_from_address,
)
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import set_address, set_contract_type, set_script, set_token_id
from src.utils.logger import LOGGER

env_manager = ConfigManager()

# Stable, wallet-independent identity of the contract TYPE. Its sha3 is the
# ``contract_hash`` peers match on; the specific wallet's `scriptPubKey` travels
# per-instance as the raw ``script`` xattr, exactly as Ergo carries propositionBytes.
CONTRACT = "p2wpkh"
CONTRACT_HASH = sha3_256(CONTRACT.encode("utf-8")).hexdigest()
LEDGER = "bitcoin"
NATIVE_ASSET = "BTC"

PROSE = (
    "Bitcoin: PoW blockchain with a UTXO model, script-based spending conditions, "
    "a fixed supply schedule, and settlement finality measured in confirmations."
)

# The proof of an incoming payment is a *confirmed transaction*, not an unspent output,
# so nothing here breaks if the receiving outputs are spent. That is what keeps this
# contract out of the deposit-generation pause Ergo needs -- and it has to stay out of
# it: one confirmation at a low fee rate routinely takes longer than that pause is
# bounded by, so being dragged into it would reject honest payments.
needs_unspent_proof = False

# How long a deposit token may sit unpaid here before it is written off. Six hours, not
# Ergo's one: a payer waiting for one confirmation at a low fee rate routinely takes
# longer than an hour, and a token that expires first rejects an honest payment **with
# the money already on-chain** -- the one direction an accounting error must never fall
# in. It lives on the contract because it describes how long this chain takes.
DEPOSIT_TOKEN_TTL = 6 * 3600

# The transaction this contract builds: one P2WPKH input, a P2WPKH output, an OP_RETURN
# and a change output. Used to turn a fee *rate* into a fee, which is what makes this
# ledger's floors a moving number rather than a constant.
VSIZE_ESTIMATE = 184

# How long to wait for the payer's confirmation, and how often to look.
WAIT_TX_TIME = 240  # attempts
WAIT_TX_SLEEP_TIME = 15  # seconds between them -- a block is ~10 minutes

payment_lock = Lock()  # Ensures the same UTXO is not spent for more than it holds.

NETWORK = lambda: str(env_manager.get("ledgers.bitcoin.NETWORK") or "mainnet")
COLD_WALLET = lambda: env_manager.get("ledgers.bitcoin.payments.COLD_WALLET") or ""
MIN_CONFIRMATIONS = lambda: max(1, int(env_manager.get("ledgers.bitcoin.payments.MIN_CONFIRMATIONS", 1) or 1))
TARGET_CONF = lambda: max(1, int(env_manager.get("ledgers.bitcoin.payments.TARGET_CONF", 6) or 6))
MAX_FEE_RATE_SAT_VB = lambda: float(env_manager.get("ledgers.bitcoin.payments.MAX_FEE_RATE_SAT_VB", 100) or 100)
RECEIVING_ADDRESS_KEY = "ledgers.bitcoin.payments.RECEIVING_ADDRESS"
# Read per call, matching the registry's own gate for the simulated contract, so the two
# can never disagree about whether this node is simulating.
SIMULATE_PAYMENTS = lambda: bool(env_manager.get("general_flags.SIMULATE_PAYMENTS"))

bitcoin_ledger = celaut_pb2.Contract.Ledger(
    tags=[LEDGER],
    prose=PROSE,
    formal="".encode("utf-8"),
)

_transaction_url_reporter: ContextVar = ContextVar("bitcoin_transaction_url_reporter", default=None)
_transaction_id_reporter: ContextVar = ContextVar("bitcoin_transaction_id_reporter", default=None)


@contextmanager
def transaction_url_reporting(reporter):
    """Temporarily report a submitted transaction's URL to the caller."""
    token = _transaction_url_reporter.set(reporter)
    try:
        yield
    finally:
        _transaction_url_reporter.reset(token)


@contextmanager
def transaction_id_reporting(reporter):
    """Temporarily report a submitted transaction's *id* to the caller.

    Kept apart from the URL hook for the same reason Ergo does: the URL is
    presentation, the id is the record `payments.tx_id` stores.
    """
    token = _transaction_id_reporter.set(reporter)
    try:
        yield
    finally:
        _transaction_id_reporter.reset(token)


def unavailable_reason() -> Optional[str]:
    """Why this contract cannot settle right now, or ``None`` when it can.

    Config and filesystem only -- no socket -- because the registry asks this on the
    payment path and on every advertisement. Two things make Bitcoin unusable, and
    neither is an error worth crashing over:

    * **No rate.** There is no default for `MU_PER_SATOSHI` and there must not be, so an
      unset one means this node is not offering Bitcoin rather than offering it at a
      price a million times wrong (see `rate.py`).
    * **No way to reach a node.** No RPC URL, or no credentials.
    """
    return rate.rate_reason() or configuration_reason()


def ledger() -> celaut_pb2.Contract.Ledger:
    """The ledger message this contract settles on, as peers receive it."""
    return bitcoin_ledger


def mu_per_unit() -> int:
    """MU bought by one whole BTC. What peers are told as ``ContractRate.mu_per_unit``."""
    return rate.mu_per_unit()


def mu_to_native(amount: int) -> Decimal:
    """MU -> satoshi, exactly. What a donation debt on this ledger is counted in."""
    return rate.mu_to_satoshi_exact(amount)


def _fee_rate_sat_vb() -> float:
    """The fee rate to build with, capped, or raise rather than pay above the cap.

    Read per call and never captured at import: this is a market price, not a constant,
    and it is the reason `settlement_floors_mu()` on this ledger is a moving number.

    The cap is refused rather than clamped. Clamping would build a transaction at a rate
    the network is not accepting, which does not fail -- it sits unconfirmed, holding a
    deposit token that eventually expires. Refusing leaves the money where it is.
    """
    cap = MAX_FEE_RATE_SAT_VB()
    estimated = backend().estimate_fee_rate(TARGET_CONF())
    if estimated is None:
        # A fresh node, or regtest with no fee history. The cap is the only figure this
        # node has been told is acceptable, so it is the honest fallback.
        LOGGER(
            f"bitcoind will not estimate a fee for {TARGET_CONF()} blocks; using the "
            f"configured ceiling of {cap} sat/vB."
        )
        return cap
    if estimated > cap:
        raise BackendUnavailable(
            f"the fee rate is {estimated:.2f} sat/vB, above the configured ceiling of "
            f"{cap} sat/vB (ledgers.bitcoin.payments.MAX_FEE_RATE_SAT_VB). Nothing was "
            "broadcast; raise the ceiling or wait for fees to fall."
        )
    return estimated


def _fee_sat() -> int:
    """What one of this contract's transactions costs right now, in satoshi."""
    return int(round(_fee_rate_sat_vb() * VSIZE_ESTIMATE))


def settlement_floors_mu() -> Tuple[int, int]:
    """``(fee, smallest payable output)`` for this ledger, in MU.

    Both are moving numbers here, unlike Ergo's constants: the fee is a market rate
    times this transaction's size, and the floor is the network's dust threshold for the
    output this contract builds. Reported in MU because the caller sizing a deposit
    (`deposits.py`) is ledger-agnostic, and per contract because Bitcoin's floors are
    orders of magnitude above Ergo's -- collapsed together they price every deposit at
    the more expensive chain (#340 §5).
    """
    return rate.satoshi_to_mu(_fee_sat()), rate.satoshi_to_mu(P2WPKH_DUST_SAT)


def get_wallet_address() -> str:
    """The one address this node is paid into, stable across restarts.

    One address, deliberately: the advertised ``script`` has to be a fixed
    `scriptPubKey` for a payer to build against, and rotating it would strand payments
    aimed at the old one. Asked of Core once and written back to the config, the same
    way this node persists the values it derives for Ergo -- so it survives a restart
    without the operator having to choose an address by hand.
    """
    configured = str(env_manager.get(RECEIVING_ADDRESS_KEY) or "").strip()
    if configured:
        if not is_valid_bitcoin_address(configured, network=NETWORK()):
            raise ValueError(
                f"{RECEIVING_ADDRESS_KEY}={configured!r} is not a valid "
                f"{NETWORK()} address. Clear it and the node will ask bitcoind for one."
            )
        return configured

    address = backend().new_address()
    if not is_valid_bitcoin_address(address, network=NETWORK()):
        raise ValueError(
            f"bitcoind returned {address!r}, which is not a valid {NETWORK()} address. "
            "Check ledgers.bitcoin.NETWORK against the node you are pointing at."
        )
    env_manager.set(RECEIVING_ADDRESS_KEY, address)
    LOGGER(f"This node will be paid in BTC at {address} (stored in config.yaml).")
    return address


def get_wallet_script() -> bytes:
    """The raw `scriptPubKey` of the receiving address: what peers are advertised."""
    script = script_pubkey_from_address(get_wallet_address(), network=NETWORK())
    if script is None:
        raise ValueError("the receiving address is not a segwit address")
    return script


def get_balance() -> Tuple[str, float]:
    """``(address, confirmed balance in BTC)``, for the operator-facing surfaces."""
    address = get_wallet_address()
    return address, float(Decimal(backend().get_balance(MIN_CONFIRMATIONS())) / 100_000_000)


def transaction_history(limit: int = 10) -> list:
    """Recent wallet transactions, in the same normalised shape Ergo's answers in.

    Core already knows which side the wallet is on -- it categorises each entry as a
    send or a receive -- so there is no box-walking to do here. What it does not report
    is the deposit token, which lives in the transaction's `OP_RETURN`, so an incoming
    row costs one extra read to say who paid. Only for the handful of rows on screen.
    """
    chain = backend()
    rows = []
    for entry in chain.list_transactions(limit):
        category = str(entry.get("category") or "")
        if category not in ("send", "receive"):
            # Immature coinbase, orphaned, or a category this node has no opinion about.
            continue
        try:
            amount_sat = abs(int(
                (Decimal(str(entry.get("amount") or 0)) * 100_000_000).to_integral_value()
            ))
        except Exception:
            amount_sat = 0
        tx_id = str(entry.get("txid") or "")
        tokens = []
        if category == "receive" and tx_id:
            try:
                tokens = _op_return_tokens(chain.raw_transaction(tx_id))
            except Exception:
                # The row is still worth showing without the token that names the payer.
                tokens = []
        counterparty = str(entry.get("address") or "")
        rows.append({
            "id": tx_id,
            "timestamp": int(entry.get("time") or 0),
            "confirmations": int(entry.get("confirmations") or 0),
            "direction": "out" if category == "send" else "in",
            "amount": amount_sat,
            "unit": NATIVE_ASSET,
            "decimals": 8,
            "counterparties": [counterparty] if counterparty else [],
            "deposit_tokens": tokens,
        })
    return rows


def init():
    """Advertise this contract: the receiving address' `scriptPubKey` as the script."""
    contract = celaut_pb2.Contract(ledger=bitcoin_ledger)
    set_token_id(contract, NATIVE_ASSET)
    # Canonical value: the raw scriptPubKey a payer builds its output against.
    set_script(contract, get_wallet_script())
    # Stable type identity for cross-node matching (its sha3 == CONTRACT_HASH).
    set_contract_type(contract, CONTRACT.encode("utf-8"))
    # Derived address for local display only; never the source of truth.
    set_address(contract, get_wallet_address())
    sql_connection.SQLConnection().add_contract(contract=contract)


def check_sender_balance(amount: int) -> bool:
    """Whether this wallet can pay ``amount`` MU plus what the transaction costs."""
    try:
        required = rate.mu_to_satoshi(amount) + _fee_sat()
        available = backend().get_balance(MIN_CONFIRMATIONS())
        if available < required:
            LOGGER(
                f"Insufficient BTC balance. Required: {satoshi_to_btc_str(required)}, "
                f"available: {satoshi_to_btc_str(available)}."
            )
            return False
        return True
    except Exception as e:
        LOGGER(f"Error checking the BTC balance: {e}")
        return False


def process_payment(amount: int, deposit_token: str, ledger: celaut_pb2.Contract.Ledger,
                    script: bytes) -> celaut_pb2.Contract:
    """Pay ``amount`` MU to ``script``, carrying ``deposit_token`` in an `OP_RETURN`.

    Returns only once the transaction has ``MIN_CONFIRMATIONS``, so the peer's validator
    sees something final and can answer accepted/rejected in one call. That is the same
    shape Ergo already has; only the wait is longer.
    """
    with payment_lock:
        amount_sat = rate.mu_to_satoshi(amount)
        LOGGER(
            f"Process bitcoin payment for token {deposit_token} of "
            f"{satoshi_to_btc_str(amount_sat)} BTC"
        )

        # The network will not relay an output below the dust threshold, so a payment
        # worth less than that cannot be settled at all. Refused here rather than
        # broadcast into a transaction nobody will carry.
        if amount_sat < P2WPKH_DUST_SAT:
            raise ValueError(
                f"Payment of {satoshi_to_btc_str(amount_sat)} BTC is below Bitcoin's "
                f"dust threshold ({satoshi_to_btc_str(P2WPKH_DUST_SAT)} BTC). Nothing "
                "can be settled for that amount; see deposits.MAX_FEE_OVERHEAD."
            )

        address = address_from_script_pubkey(script, network=NETWORK())
        if not address:
            raise ValueError(
                "the peer's advertised script is not a segwit scriptPubKey, so there is "
                "no address to pay"
            )

        chain = backend()
        tx_id = chain.send_to(
            address,
            amount_sat,
            # The deposit token, as bytes. This is Bitcoin's `R4`: it is what ties this
            # transaction to the deposit the receiver is expecting.
            op_return=deposit_token.encode("utf-8"),
            fee_rate_sat_vb=_fee_rate_sat_vb(),
        )
        url = f"https://mempool.space/tx/{tx_id}"
        LOGGER(f"Transaction submitted: {url} for token {deposit_token}")
        reporter = _transaction_url_reporter.get()
        if reporter:
            reporter(url)
        id_reporter = _transaction_id_reporter.get()
        if id_reporter:
            id_reporter(tx_id)

        wanted = MIN_CONFIRMATIONS()
        for _ in range(WAIT_TX_TIME):
            sleep(WAIT_TX_SLEEP_TIME)
            try:
                status = chain.tx_status(tx_id)
            except BackendUnavailable as e:
                LOGGER(f"Could not read tx {tx_id} yet: {e}")
                continue
            confirmations = int(status.get("confirmations", 0) or 0)
            if confirmations < 0:
                # Core says the transaction was replaced or reorged out. That is not
                # "not yet confirmed": waiting longer cannot make it true again.
                raise ValueError(
                    f"Transaction {tx_id} was replaced or left the chain "
                    f"({confirmations} confirmations); nothing was credited."
                )
            if confirmations >= wanted:
                LOGGER(f"Tx {tx_id} verified with {confirmations} confirmation(s).")
                contract = celaut_pb2.Contract(ledger=ledger)
                set_token_id(contract, NATIVE_ASSET)
                set_script(contract, script)
                set_contract_type(contract, CONTRACT.encode("utf-8"))
                return contract

        raise TimeoutError(
            f"Transaction {tx_id} did not reach {wanted} confirmation(s) in "
            f"{WAIT_TX_TIME * WAIT_TX_SLEEP_TIME // 60} minutes. The money is on-chain; "
            "the peer has not been told."
        )


def _op_return_tokens(transaction: dict) -> list:
    """Every `OP_RETURN` payload in ``transaction``, decoded as text where possible."""
    payloads = []
    for output in transaction.get("vout") or []:
        script = (output.get("scriptPubKey") or {})
        if script.get("type") != "nulldata":
            continue
        asm = str(script.get("asm") or "")
        parts = asm.split()
        if len(parts) < 2:
            continue
        try:
            payloads.append(bytes.fromhex(parts[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    return payloads


def _paid_to_script(transaction: dict, script_hex: str) -> int:
    """Total satoshi ``transaction`` paid to ``script_hex``.

    Every matching output, summed. Anything else the transaction does -- change back to
    the payer, an output to a third party -- is not a payment to this node.
    """
    total = 0
    for output in transaction.get("vout") or []:
        script = (output.get("scriptPubKey") or {})
        if str(script.get("hex") or "").lower() != script_hex:
            continue
        try:
            total += int((Decimal(str(output.get("value") or 0)) * 100_000_000).to_integral_value())
        except Exception:
            continue
    return total


def payment_process_validator(amount: int, token: str, ledger: celaut_pb2.Contract.Ledger,
                              script: bytes) -> bool:
    """Whether a confirmed transaction paid us ``amount`` MU carrying ``token``.

    Four things have to hold, and the fourth is the one worth stating: the ledger is
    ours, the script is *our* receiving script, a confirmed transaction carries the
    deposit token in an `OP_RETURN`, and it paid **at least** the expected amount.

    At least, not exactly: the payer converts our MU figure from its own scale and has
    to round down to a whole MU of ours, so a correct payment routinely carries a little
    more than the credit it asks for. Demanding equality would reject payments with the
    money already on-chain. More than asked for is never a problem -- the credit is what
    was asked for, and the rest is simply kept.
    """
    try:
        assert LEDGER in ledger.tags, "Ledger does not match"

        script_hex = bytes(script).hex().lower()
        assert script_hex == get_wallet_script().hex().lower(), \
            "Contract script does not match this node's receiving script"

        chain = backend()
        address = get_wallet_address()
        expected = rate.mu_to_satoshi(amount)
        min_conf = MIN_CONFIRMATIONS()

        for entry in chain.list_received(address, min_conf):
            for tx_id in entry.get("txids") or []:
                try:
                    transaction = chain.raw_transaction(tx_id)
                except BackendUnavailable as e:
                    # Could not look, which is not the same as "did not pay": leave the
                    # rest of the candidates a chance rather than answering no.
                    LOGGER(f"Could not read bitcoin tx {tx_id}: {e}")
                    continue
                if token not in _op_return_tokens(transaction):
                    continue
                paid = _paid_to_script(transaction, script_hex)
                if paid >= expected:
                    LOGGER(
                        f"Bitcoin payment for token {token} verified: "
                        f"{satoshi_to_btc_str(paid)} BTC in {tx_id}."
                    )
                    return True
                LOGGER(
                    f"Insufficient amount for token {token}. Was "
                    f"{satoshi_to_btc_str(paid)} BTC, expected at least "
                    f"{satoshi_to_btc_str(expected)} BTC."
                )
                return False

        LOGGER(f"No confirmed bitcoin transaction carries the token {token}.")
        return False
    except Exception as e:
        LOGGER(f"Error validating bitcoin payment process: {e}")
        return False


def manager():
    """One periodic tick for this contract: pay what is owed, then sweep what is spare.

    Same order and same reasoning as Ergo's: a donation is a debt this node already
    incurred against payments it was paid, while the sweep only moves its own funds
    between its own wallets and pays nobody.
    """
    _pay_accrued_donations()
    _sweep_to_cold_wallet()


def _pay_accrued_donations():
    """Pay the donation debt accrued on this contract, if a transaction is worth making.

    The order and the bookkeeping are shared (`donations.payout`); what is Bitcoin's is
    the money -- a fee that moves with the market, the network's dust threshold, how a
    satoshi figure reads, and how to send.
    """
    from src.payment_system.donations import config as donation_config
    from src.payment_system.donations.payout import pay_accrued

    try:
        fee = _fee_sat()
    except Exception as e:
        LOGGER(f"Not paying BTC donations this tick: {e}")
        return

    def send(outputs, fee_sat: int) -> str:
        chain = backend()
        if len(outputs) == 1:
            address, amount = outputs[0]
            return chain.send_to(address, amount, fee_rate_sat_vb=fee / VSIZE_ESTIMATE)
        # Several wallets: one transaction with one output each, so a split costs one
        # fee rather than one per recipient.
        return chain.send_many(outputs, fee_rate_sat_vb=fee / VSIZE_ESTIMATE)

    def can_cover(total_sat: int) -> bool:
        return backend().get_balance(MIN_CONFIRMATIONS()) >= total_sat

    pay_accrued(
        ledger=LEDGER,
        contract_hash=CONTRACT_HASH,
        asset=NATIVE_ASSET,
        fee=fee,
        minimum_output=P2WPKH_DUST_SAT,
        min_transfer=btc_to_satoshi(donation_config.min_transfer(LEDGER, NATIVE_ASSET)),
        send=send,
        render=lambda amount: f"{satoshi_to_btc_str(amount)} BTC",
        to_mu=rate.satoshi_to_mu,
        valid_address=lambda address: is_valid_bitcoin_address(address, network=NETWORK()),
        simulate=SIMULATE_PAYMENTS(),
        available=can_cover,
        lock=payment_lock,
    )


def _sweep_to_cold_wallet():
    """Move the wallet's excess to cold storage when both thresholds are met."""
    LOGGER("Exec bitcoin interface manager (cold sweep).")
    try:
        cold_wallet = COLD_WALLET()
        if not cold_wallet:
            LOGGER("No BTC cold wallet configured; skipping sweep.")
            return
        if not is_valid_bitcoin_address(cold_wallet, network=NETWORK()):
            # Refused at load, so this is the config having changed under a running
            # node. Refusing beats sweeping savings to an address nobody can spend.
            LOGGER(
                f"[ERROR] ledgers.bitcoin.payments.COLD_WALLET is not a valid "
                f"{NETWORK()} address; not sweeping."
            )
            return

        chain = backend()
        fee = _fee_sat()
        sweep_sat = compute_sweep_amount(
            balance=chain.get_balance(MIN_CONFIRMATIONS()),
            hot_limit=btc_to_satoshi(
                env_manager.get("ledgers.bitcoin.payments.HOT_WALLET_LIMITS") or 0
            ),
            min_transfer=btc_to_satoshi(
                env_manager.get("ledgers.bitcoin.payments.COLD_WALLET_MIN_TRANSFER") or 0
            ),
            fee=fee,
            technical_min=P2WPKH_DUST_SAT,
        )
        if sweep_sat is None:
            LOGGER("Nothing to sweep on bitcoin.")
            return
        if SIMULATE_PAYMENTS():
            LOGGER(
                f"SIMULATE_PAYMENTS is on: would sweep "
                f"{satoshi_to_btc_str(sweep_sat)} BTC to {cold_wallet}."
            )
            return

        with payment_lock:
            tx_id = chain.send_to(
                cold_wallet, sweep_sat, fee_rate_sat_vb=fee / VSIZE_ESTIMATE
            )
        LOGGER(
            f"Cold sweep tx -> {tx_id}: {satoshi_to_btc_str(sweep_sat)} BTC to "
            f"{cold_wallet} (fee {satoshi_to_btc_str(fee)} BTC)."
        )
    except Exception as e:
        LOGGER(f"Exception on bitcoin cold sweep -> {e}")
