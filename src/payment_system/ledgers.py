from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_ledger_and_contract_addr_from_contract
from src.payment_system.contracts.ergo.interface import CONTRACT_HASH, CONTRACT
from src.utils.env import EnvManager
from src.utils.utils import to_gas_amount

ERGO_GAS_COST = EnvManager().get_env("ERGO_GAS_COST")

def local_payment_methods() -> Generator[celaut.GasPrice, None, None]:

    for address, ledger in get_ledger_and_contract_addr_from_contract(CONTRACT_HASH):

        contract_ledger = celaut.ContractLedger()
        contract_ledger.contract = CONTRACT
        contract_ledger.contract_addr, contract_ledger.ledger = address, ledger

        gas_price = celaut.GasPrice(
            contract_ledger=contract_ledger,
            gas_amount=celaut.GasAmount(n=str(ERGO_GAS_COST))  # to_gas_amount(ERGO_GAS_COST)
        )

        yield gas_price
