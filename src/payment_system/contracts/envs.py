from textwrap import dedent
from typing import Callable, Dict
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


def check_sender_balances() -> Dict[contract_hash, Callable[[amount], bool]]:
    ergo = _ergo_interface()
    return {
        **({simulated.CONTRACT_HASH: simulated.check_sender_balance} if SIMULATED else {}),
        ergo.CONTRACT_HASH: ergo.check_sender_balance
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
