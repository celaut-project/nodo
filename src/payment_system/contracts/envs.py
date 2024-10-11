from textwrap import dedent
from typing import Callable, Dict
from protos import celaut_pb2

from src.payment_system.contracts.simulator import interface as simulated
from src.payment_system.contracts.ergo import interface as ergo
from src.utils.env import EnvManager

SIMULATED = bool(EnvManager().get_env("SIMULATE_PAYMENTS"))

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
    **({simulated.CONTRACT_HASH: simulated.process_payment} if SIMULATED else {}),
    ergo.CONTRACT_HASH: ergo.process_payment
}

INIT_INTERFACES: Dict[contract_hash, Callable[[], None]] = {
    ergo.CONTRACT_HASH: ergo.init
}

MANAGE_INTERFACES: Dict[contract_hash, Callable[[], None]] = {
    ergo.CONTRACT_HASH: ergo.manager
}

DEMOS = [simulated.CONTRACT_HASH] if SIMULATED else []

def print_payment_info() -> str:
    main, aux = ergo.get_ergo_info()
    ergo_addr, ergo_amount = main
    aux_addr, aux_amount = aux
    return dedent(f"""
    Ergo Platform:

    1. **Sending Wallet Address**: {ergo_addr} (for processing payments)
    2. **Amount to Send**: {ergo_amount} ERGs

    3. **Receiver Wallet Address**: {aux_addr} (public wallet for client payments)
    4. **Amount Received**: {aux_amount} ERGs

    5. **Total Transaction Amount**: {ergo_amount + aux_amount} ERGs

    **Important Note**: The node periodically transfers funds from the Receiver Wallet to the Sending Wallet, where most deposits accumulate. 
    To increase the node's deposit, please send funds to the Sending Wallet Address.
    """)

