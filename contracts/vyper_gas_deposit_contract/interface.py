import json
from multiprocessing import Lock
from contracts.main.utils import transact
from main import utils, singleton
from typing import Dict
from web3 import Web3
from hashlib import sha256
import gateway_pb2
from time import sleep

CONTRACT_HASH: bytes = sha256(open('bytecode', 'rb').read().encode('utf-8')).digest()

# Vyper gas deposit contract, used to deposit gas to the contract. Inherent from the ledger and contract id.

class LedgerContractInterface:

    def __init__(self, w3_generator, contract_addr):
        self.w3: Web3 = next(w3_generator)
        self.contract_addr: str = contract_addr
        
        self.generate_contract = lambda addr: self.w3.eth.contract(
            address = Web3.toChecksumAddress(addr),
            abi = json.load(open('abi.json')), 
            bytecode = open('bytecode', 'rb').read()            
        )
        self.contract = self.generate_contract(addr = contract_addr)

        self.sessions: Dict[bytes, int] = {}
        self.sessions_lock = Lock()

        self.poll_interval: int = 2

        # Update Session Event.
        utils.catch_event(
            contractAddress = Web3.toChecksumAddress(contract_addr),
            w3 = self.w3,
            contract = self.contract,
            event_name = 'NewSession',
            opt = lambda args: self.__new_session(
                        token = args['token'], 
                        amount = args['gas_amount']
                    ),
            poll_interval = self.poll_interval,
        )


    def __new_session(self, token, amount):
        print('New session:', token, amount)
        self.sessions_lock.acquire()
        if token not in self.sessions:
            self.sessions[token] = amount
        else:
            self.sessions[token] += amount
        self.sessions_lock.release()


    def validate_session(self, token, amount) -> bool:
        sleep(self.poll_interval)
        if self.sessions[token] >= amount:
            self.sessions_lock.acquire()
            self.sessions[token] -= amount
            self.sessions_lock.release()
            return True
        return False


    def add_gas(self, token: str, amount: int, contract_addr: str) -> str:
        return transact(
            w3 = self.w3,
            method = self.generate_contract(addr = contract_addr).functions.add_gas(
                sha256(token.encode('utf-8')).digest(), 
                amount
            ),
            priv = self.priv,
            value = 20
        )


# Singleton class
class VyperDepositContractInterface(singleton.Singleton):

    def __init__(self):
        self.ledger_providers: Dict[str: LedgerContractInterface] = {}
        for ledger, contract_address in utils.get_interface_ledgers_from_mongodb('vyper_deposit_contract').items():
            self.ledger_providers[ledger] = LedgerContractInterface(
                w3_generator = utils.w3_generator_factory(ledger = ledger),
                contract_addr = contract_address
            )

    # TODO si necesitas añadir un nuevo ledger, deberás reiniciar el nodo, a no ser que se implemente un método set_ledger_on_interface()

    def process_payment(self, amount: int, token: str, ledger: str, contract_addr: str) -> gateway_pb2.ContractLedger:
        print("Processing payment...")
        ledger_provider = self.ledger_providers[ledger]
        ledger_provider.add_gas(token, amount, contract_addr)
        return gateway_pb2.ContractLedger(
            ledger = ledger,
            contract_addr = contract_addr
        )


    def payment_process_validator(self, amount: int, token: str, ledger: str, contract_addr: str) -> bool:
        print("Validating payment...")
        ledger_provider = self.ledger_providers[ledger]
        assert contract_addr == ledger_provider.contract_addr
        return ledger_provider.validate_session(token, amount) 


def process_payment(amount: int, token: str, ledger: str, contract_address: str) -> gateway_pb2.ContractLedger:
    return VyperDepositContractInterface().process_payment(amount, token, ledger, contract_address)

def payment_process_validator(amount: int, token: str, ledger: str, contract_addr: str) -> bool:
    return VyperDepositContractInterface().payment_process_validator(amount, token, ledger, contract_addr)