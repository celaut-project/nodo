from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_ledger_and_contract_addr_from_contract
from src.payment_system.contracts.ergo.interface import CONTRACT_HASH, CONTRACT

def local_payment_methods() -> Generator[celaut.ContractLedger, None, None]:
    for address, ledger in get_ledger_and_contract_addr_from_contract(CONTRACT_HASH):
        contract_ledger = celaut.ContractLedger()
        contract_ledger.contract = CONTRACT
        contract_ledger.contract_addr, contract_ledger.ledger = address, ledger
        yield contract_ledger
