from typing import Generator
from protos import celaut_pb2 as celaut
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.payment_system.contracts.ergo.interface import CONTRACT_HASH, CONTRACT
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount
from src.utils.logger import LOGGER
from src.utils.contract_xattrs import set_address, set_script, set_token_id

GAS_PER_ERG = int(ConfigManager().get("ledgers.ergo.GAS_PER_ERG"))
 
def local_payment_methods() -> Generator[celaut.GasPrice, None, None]:
    for script, ledger in get_peer_contract_instances(CONTRACT_HASH): 

        address = script.decode("utf-8")

        ledger_tag = ledger.tags[0] if ledger.tags else "unknown"
        LOGGER(f"Using ledger {ledger_tag} with address {address} for contract {CONTRACT_HASH}")

        contract_ledger = celaut.Contract()
        contract_ledger.ledger.CopyFrom(ledger)
        set_script(contract_ledger, CONTRACT.encode("utf-8"))
        set_address(contract_ledger, address)
        set_token_id(contract_ledger, "ERG")

        gas_price = celaut.GasPrice(
            contract=contract_ledger,
            gas_amount=to_gas_amount(GAS_PER_ERG)
        )

        yield gas_price
