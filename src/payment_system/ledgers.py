from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.payment_system.contracts.ergo.interface import CONTRACT_HASH, CONTRACT
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount
from src.utils.logger import LOGGER

GAS_PER_ERG = int(ConfigManager().get("ledgers.ergo.GAS_PER_ERG"))
 
def local_payment_methods() -> Generator[celaut.GasPrice, None, None]:
    for script, ledger in get_peer_contract_instances(CONTRACT_HASH): 

        address = script.decode("utf-8")

        LOGGER(f"Using ledger {ledger} with address {address} for contract {CONTRACT_HASH}")

        contract_ledger = celaut.Contract()
        contract_ledger.template.formal = CONTRACT.encode("utf-8")
        contract_ledger.script = script
        contract_ledger.ledger.CopyFrom(ledger)

        gas_price = celaut.GasPrice(
            contract=contract_ledger,
            gas_amount=to_gas_amount(GAS_PER_ERG)
        )

        yield gas_price