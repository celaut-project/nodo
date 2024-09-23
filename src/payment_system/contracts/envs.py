from typing import Callable, Dict
from protos import celaut_pb2

from src.payment_system.contracts.simulator import interface as simulated
from src.payment_system.contracts.ergo import interface as ergo
from src.utils.env import EnvManager
from src.utils.logger import LOGGER

SIMULATED = bool(EnvManager().get_env("SIMULATE_PAYMENTS"))
LOGGER(f"Simulate payments: {SIMULATED}")

contract_hash = str
contract_addr = str
token = str
ledger = str
tx_id = str
amount = int
validate_token = Callable[[token], bool]
contract_ledger = celaut_pb2.Service.Api.ContractLedger

PAYMENT_PROCESS_VALIDATORS = {
    **({simulated.CONTRACT_HASH: simulated.payment_process_validator} if SIMULATED else {}),
    ergo.CONTRACT_HASH: ergo.payment_process_validator
}

AVAILABLE_PAYMENT_PROCESS: Dict[contract_hash, Callable[[amount, token, ledger, contract_addr], contract_ledger]] = {
    simulated.CONTRACT_HASH: simulated.process_payment,
    **({simulated.CONTRACT_HASH: simulated.process_payment} if SIMULATED else {}),
    ergo.CONTRACT_HASH: ergo.process_payment
}

INIT_INTERFACES: Dict[contract_hash, Callable[[], None]] = {
    ergo.CONTRACT_HASH: ergo.init
}

MANAGE_INTERFACES: Dict[contract_hash, Callable[[], None]] = {
    ergo.CONTRACT_HASH: ergo.manager
}

DEMOS = [simulated.CONTRACT_HASH]
