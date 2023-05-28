import hashlib
from types import FunctionType
from typing import Dict

from contracts.vyper_gas_deposit_contract import interface as vyper_gdc

SHA3_256_ID = bytes.fromhex("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")

PAYMENT_PROCESS_VALIDATORS: Dict[bytes, FunctionType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.payment_process_validator}  # contract_hash:  lambda peer_id, tx_id, amount -> bool,

AVAILABLE_PAYMENT_PROCESS: Dict[bytes, FunctionType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.process_payment}  # contract_hash:   lambda amount, peer_id -> tx_id,
