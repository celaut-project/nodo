from types import FunctionType
from typing import Dict

# from contracts.ethereum.deposit_contract import interface   # SI SE ACTIVA DEBE ACTIVARSE TAMBBIEN manager/payment_process.py init_contract_interfaces()
from src.payment_system.contracts.ethereum.deposit_contract.simulator import interface

PAYMENT_PROCESS_VALIDATORS: Dict[str, FunctionType] = {
    interface.CONTRACT_HASH: interface.payment_process_validator}  # contract_hash:  lambda peer_id, tx_id, amount -> bool,

AVAILABLE_PAYMENT_PROCESS: Dict[str, FunctionType] = {
    interface.CONTRACT_HASH: interface.process_payment}  # contract_hash:   lambda amount, peer_id -> tx_id,
