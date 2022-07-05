from web3.middleware import geth_poa_middleware
from web3 import HTTPProvider, Web3


def check_provider_availability(provider) -> bool:
    return True  # TODO check if the provider is avialable.

def w3_generator_factory(ledger: str):
    while True:
        for provider in get_ledger_providers_from_mongodb(ledger = ledger):
            if not check_provider_availability(provider = provider): continue
            w3  = Web3(HTTPProvider(provider))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            yield w3

def get_interface_ledgers_from_mongodb(interface_id) -> dict:
    raise NotImplementedError

def get_ledger_providers_from_mongodb(ledger: str) -> list:
    pass

def set_ledger_on_mongodb(ledger: str):
    pass

def set_provider_on_mongodb(provider: str, ledger: str):
    pass

def set_ledger_contract_on_mongodb(ledger: str, contract_addr: str):
    pass

def get_ledger_contract_from_mongodb(ledger: str) -> str:
    pass