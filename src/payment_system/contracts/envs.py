from typing import Callable, Dict
from protos import celaut_pb2

from src.payment_system.contracts.simulator import interface as simulated

contract_hash = str
contract_addr = str
token = str
ledger = str
tx_id = str
amount = int
validate_token = Callable[[token], bool]
contract_ledger = celaut_pb2.Service.Api.ContractLedger

PAYMENT_PROCESS_VALIDATORS: Dict[contract_hash, Callable[[amount, token, ledger, contract_addr, validate_token], bool]] = {
    simulated.CONTRACT_HASH: simulated.payment_process_validator
}

AVAILABLE_PAYMENT_PROCESS: Dict[contract_hash, Callable[[amount, token, ledger, contract_addr], contract_ledger]] = {
    simulated.CONTRACT_HASH: simulated.process_payment
}
