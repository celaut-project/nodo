import json
from multiprocessing import Lock
from main import utils, singleton
from typing import Dict
from web3 import Web3


# Vyper gas deposit contract, used to deposit gas to the contract. Inherent from the ledger and contract id.

class LedgerContractInterface:

    def __init__(self, w3_generator, contract_addr):
        self.w3 = next(w3_generator)
        self.contract_addr = contract_addr
        
        self.contract = lambda w3: w3.eth.contract(
            abi = json.load(open('abi.json')),
            bytecode = json.load(open('bytecode.json')),
        )

        print('Init session on contract:', contract_addr)

        self.contract = self.w3.eth.contract(
            address = Web3.toChecksumAddress(contract_addr),
            abi = json.load(open('abi.json')), 
            bytecode = open('bytecode', 'rb').read()
        )

        print('Init session on contract:', contract_addr)

        self.sessions = {}
        self.sessions_lock = Lock()

        # Update Session Event.
        utils.catch_event(
            contractAddress = Web3.toChecksumAddress(contract_addr),
            w3 = self.w3,
            contract = self.contract,
            event_name = 'NewSession',
            opt = lambda args: self.__new_session(
                        token = args['token'], 
                        amount = args['gas_amount']
                    )
        )


    def __new_session(self, token, amount):
        print('New session:', token, amount)
        self.sessions_lock.acquire()
        if token not in self.sessions:
            self.sessions[token] = amount
        else:
            self.sessions[token] += amount
        self.sessions_lock.release()



    def transfer_property(self, new_owner):
        self.w3.eth.wait_for_transaction_receipt(
            self.contract(self.w3).functions.transfer_property(
                new_owner = new_owner
            ).transact()
        )

    def validate_session(self, token, amount) -> bool:
        if self.sessions[token] >= amount:
            self.sessions_lock.acquire()
            self.sessions[token] -= amount
            self.sessions_lock.release()
            return True
        return False


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

    def process_payment(self, amount: int, peer_id: int, ledger: str, contract_id: str) -> str:
        print("Processing payment...")
        return '0x0000000000000000000000000000000000000000'  # TODO


    def payment_process_validator(self, amount: int, token: str, ledger: str, contract_id: str) -> bool:
        print("Validating payment...")
        assert contract_id == utils.get_ledger_contract_from_mongodb(ledger)
        return self.ledger_providers(ledger).validate_session(token, amount) 


def process_payment(amount: int, peer_id: int, ledger, contract_id) -> str:
    return VyperDepositContractInterface().process_payment(amount, peer_id, ledger, contract_id)

def payment_process_validator(amount: int, token: str, ledger: str, contract_id: str) -> bool:
    return VyperDepositContractInterface().payment_process_validator(amount, token, ledger, contract_id)