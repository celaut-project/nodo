from types import FunctionType
from typing import Dict

from contracts.vyper_gas_deposit_contract import interface as vyper_gdc

PAYMENT_PROCESS_VALIDATORS: Dict[bytes, FunctionType] = {
vyper_gdc.CONTRACT_HASH: vyper_gdc.payment_process_validator}  # contract_hash:  lambda peer_id, tx_id, amount -> bool,

AVAILABLE_PAYMENT_PROCESS: Dict[bytes, FunctionType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.process_payment}  # contract_hash:   lambda amount, peer_id -> tx_id,

