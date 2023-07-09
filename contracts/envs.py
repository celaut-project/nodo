from types import FunctionType
from typing import Dict

# from contracts.vyper_gas_deposit_contract import interface as vyper_gdc   # SI SE ACTIVA DEBE ACTIVARSE TAMBBIEN manager/payment_process.py init_contract_interfaces()
from contracts.vyper_gas_deposit_contract.simulator import interface as vyper_gdc

PAYMENT_PROCESS_VALIDATORS: Dict[str, FunctionType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.payment_process_validator}  # contract_hash:  lambda peer_id, tx_id, amount -> bool,

AVAILABLE_PAYMENT_PROCESS: Dict[str, FunctionType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.process_payment}  # contract_hash:   lambda amount, peer_id -> tx_id,
