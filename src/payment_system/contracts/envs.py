from textwrap import dedent
from typing import Any, Callable, Dict, Tuple
from contextlib import nullcontext
from protos import celaut_pb2

from src.payment_system.contracts.simulator import interface as simulated
from src.utils.config import ConfigManager
from src.utils.java_dependency import JavaDependencyMissing, build_java_dependency_message

SIMULATED = ConfigManager().get("SIMULATE_PAYMENTS")

contract_hash = str
script = bytes
token = str
ledger = str
tx_id = str
amount = int
validate_token = Callable[[token], bool]
contract_ledger = celaut_pb2.Contract

def _ergo_interface():
    try:
        from src.payment_system.contracts.ergo import interface as ergo
        return ergo
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise JavaDependencyMissing(build_java_dependency_message(feature="Ergo payments or reputation")) from exc


def payment_process_validators() -> Dict[contract_hash, validate_token]:
    ergo = _ergo_interface()
    return {
        **({simulated.CONTRACT_HASH: simulated.payment_process_validator} if SIMULATED else {}),
        ergo.CONTRACT_HASH: ergo.payment_process_validator
    }


def available_payment_process() -> Dict[contract_hash, Callable[[amount, token, ledger, script], contract_ledger]]:
    ergo = _ergo_interface()
    return {
        **({simulated.CONTRACT_HASH: simulated.process_payment} if SIMULATED else {}),
        ergo.CONTRACT_HASH: ergo.process_payment
    }


def transaction_url_reporting(reporter):
    """Provide the current payment flow with an Ergo transaction URL reporter."""
    if SIMULATED:
        return nullcontext()
    return _ergo_interface().transaction_url_reporting(reporter)


def transaction_id_reporting(reporter):
    """Provide the current payment flow with a transaction id reporter.

    A simulated payment has no transaction and reports nothing, so the payment it
    records carries no id -- which is the truth about it.
    """
    if SIMULATED:
        return nullcontext()
    return _ergo_interface().transaction_id_reporting(reporter)


def check_sender_balances() -> Dict[contract_hash, Callable[[amount], bool]]:
    ergo = _ergo_interface()
    return {
        **({simulated.CONTRACT_HASH: simulated.check_sender_balance} if SIMULATED else {}),
        ergo.CONTRACT_HASH: ergo.check_sender_balance
    }


def display_units() -> Dict[str, Dict[str, Any]]:
    """Display units the payment contracts contribute, keyed by unit name.

    Lets `monetary.display_unit` offer the operator the unit their payment system settles
    in without the accounting core naming a ledger. Each entry has the shape of a
    hand-declared `ui.UNITS.<name>` block, except that its rate is derived from the ledger
    and so cannot go stale.

    Only the *light* rate module is imported, never `ergo.interface`: this is reached from
    `format_mu`, which runs on log lines all over the node.

    A payment stack that will not import contributes nothing rather than raising -- money
    still gets rendered, in raw MU. A rate that is present but *malformed* does raise: that
    is a configuration error, and quietly falling back to MU would hide it.
    """
    units: Dict[str, Dict[str, Any]] = {}
    try:
        from src.payment_system.contracts.ergo import rate as ergo_rate
    except (ImportError, ModuleNotFoundError, OSError):
        return units
    units.update(ergo_rate.display_units())
    return units


def settlement_floors() -> Dict[contract_hash, Callable[[], Tuple[amount, amount]]]:
    """Per contract: ``(fee, smallest payable output)`` in MU.

    What a deposit has to clear before it can be settled at all. Kept here with the rest
    of the per-contract dispatch so `deposits.py` can size a deposit without naming a
    ledger -- it used to import Ergo's `DEFAULT_FEE` and `SAFE_MIN_BOX_VALUE` directly,
    which put a chain-specific floor on every payment system, including the simulated one.
    """
    ergo = _ergo_interface()
    return {
        **({simulated.CONTRACT_HASH: simulated.settlement_floors_mu} if SIMULATED else {}),
        ergo.CONTRACT_HASH: ergo.settlement_floors_mu
    }


def init_interfaces() -> Dict[contract_hash, Callable[[], None]]:
    ergo = _ergo_interface()
    return {
        ergo.CONTRACT_HASH: ergo.init
    }


def manage_interfaces() -> Dict[contract_hash, Callable[[], None]]:
    ergo = _ergo_interface()
    return {
        ergo.CONTRACT_HASH: ergo.manager
    }

DEMOS = [simulated.CONTRACT_HASH] if SIMULATED else []

def print_payment_info() -> str:
    ergo = _ergo_interface()
    address, amount = ergo.get_balance()
    cold_wallet = ergo.COLD_WALLET()

    info = f"Wallet: {address}, Amount: {amount} ERGs \n"
    if cold_wallet:
        info += f"Cold Wallet: {cold_wallet} \n"
    return info
