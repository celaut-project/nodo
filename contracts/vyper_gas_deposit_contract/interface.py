import sys, os;

from src.utils.singleton import Singleton

sys.path.append(os.getcwd())
from threading import Thread

import json
from multiprocessing import Lock

from time import sleep, time
from typing import Dict
from web3 import Web3, exceptions
from hashlib import sha256

from protos import celaut_pb2, gateway_pb2
from src.utils.logger import LOGGER
from contracts.eth_main.utils import get_priv_from_ledger, transact, w3_generator_factory, \
        get_ledger_and_contract_addr_from_contract, catch_event

DIR = os.getcwd() + '/contracts/vyper_gas_deposit_contract/'
CONTRACT: bytes = open(DIR+'bytecode', 'rb').read()
CONTRACT_HASH: bytes = sha256(CONTRACT).digest()

# Vyper gas deposit contract, used to deposit gas to the contract. Inherent from the ledger and contract id.

gas_to_contract = lambda amount, parity_factor: int(amount / 10**parity_factor)
class LedgerContractInterface:

    def __init__(self, w3_generator, contract_addr, priv):
        print(int(time()), 'EVM gas deposit contract interface init for '+ str(contract_addr))
        self.w3: Web3 = next(w3_generator)
        self.contract_addr: str = contract_addr

        # TODO debe de ir en una clase para el Ledger, sin un contrato concreto.
        self.priv = priv
        self.pub = self.w3.eth.account.from_key(priv).address
        self.transaction_lock = Lock()
        self.last_nonce: int = 0
        self.nonce_count: int = 0
        
        self.generate_contract = lambda addr: self.w3.eth.contract(
            address = Web3.toChecksumAddress(addr),
            abi = json.load(open(DIR+'abi.json')), 
            bytecode = open(DIR+'bytecode', 'rb').read()            
        )
        self.contract = self.generate_contract(addr = contract_addr)

        self.sessions: Dict[bytes, int] = {}
        self.sessions_lock = Lock()
        
        # TODO this variables depends on the ledger, so they should be moved on the mongo.contracts collection OR be dynamically updated.
        self.poll_interval: int = 2
        self.poll_iterations: int = 60
        self.poll_init_delay: int = 20
        self.wait_mint_timeout: int = 120
        self.wait_mint_poll_latency: float = 0.1

        self.contract_to_gas = lambda amount: amount * 10**self.contract.functions.get_parity_factor().call()

        Thread(target=self.catch_event_thread, args=(contract_addr,)).start()
        

    def get_nonce(self) -> int:
        with self.transaction_lock:
            last_nonce = self.w3.eth.getTransactionCount(self.pub)
            if last_nonce == self.last_nonce:
                self.nonce_count += 1    
            else:
                self.nonce_count = 0
                self.last_nonce = last_nonce
            LOGGER('             nonce:'+ str(self.nonce_count + last_nonce))
            return self.nonce_count + last_nonce

    # Update Session Event.
    def catch_event_thread(self, contract_addr):
        catch_event(
            contract_address = Web3.toChecksumAddress(contract_addr),
            w3 = self.w3,
            contract = self.contract,
            event_name = 'NewSession',
            init_delay = self.poll_init_delay,
            opt = lambda args: self.__new_session(
                        token = args['token'], 
                        amount = args['amount']
                    ),
            poll_interval = self.poll_interval,
        )

    def __new_session(self, token, amount):
        amount = self.contract_to_gas(amount)
        with self.sessions_lock:
            if token not in self.sessions:
                self.sessions[token] = amount
            else:
                self.sessions[token] += amount
            print('\n',int(time()),'New session:', token, amount, '\n')


    def validate_session(self, token: str, amount: int, validate_token = None) -> bool:
        token_encoded = sha256(token.encode('utf-8')).digest()
        for i in range(self.poll_iterations):
            if token_encoded in self.sessions and self.sessions[token_encoded] >= amount and \
                ( not validate_token or validate_token(token)):
                with self.sessions_lock: 
                    self.sessions[token_encoded] -= amount
                return True 
            else: 
                sleep(self.poll_interval)
        print('Session not found', self.sessions,'\n', token, token_encoded, amount, '\n',
                 token_encoded in self.sessions, self.sessions[token_encoded] >= amount, '\n',
                 not validate_token, validate_token(token), '\n'
                 )
        return False


    def add_gas(self, token: str, amount: int, contract_addr: str) -> str:
        contract = self.generate_contract(addr = contract_addr)
        while True: 
            nonce = self.get_nonce()   
            try:
                return transact(
                    w3 = self.w3,
                    method = contract.functions.add_gas(
                        sha256(token.encode('utf-8')).digest(),
                    ),
                    priv = self.priv,
                    nonce = nonce,
                    value = gas_to_contract(amount, contract.functions.get_parity_factor().call()),
                    timeout = self.wait_mint_timeout,
                    poll_latency = self.wait_mint_poll_latency,
                )
            except exceptions.TimeExhausted:
                print('Timeout while adding gas for token: ', token, '\n')
                return ''
            except Exception as e:
                if str(e) == "{'code': -32000, 'message': 'already known'}":
                    print('Transaction already known: ', token, '\n')
                    continue
                else:
                    print('Error '+str(e)+' while adding gas for token: ', token, '\n')
                    return ''


# Singleton class
class VyperDepositContractInterface(metaclass=Singleton):

    def __init__(self):
        self.ledger_providers: Dict[str: LedgerContractInterface] = {}
        for d in get_ledger_and_contract_addr_from_contract(contract_hash = CONTRACT_HASH):
            ledger, contract_address = d.values()
            self.ledger_providers[ledger] = LedgerContractInterface(
                w3_generator = w3_generator_factory(ledger = ledger),
                contract_addr = contract_address,
                priv = get_priv_from_ledger(ledger)
            )

    # TODO si necesitas añadir un nuevo ledger, deberás reiniciar el nodo, a no ser que se implemente un método set_ledger_on_interface()

    def process_payment(self, amount: int, token: str, ledger: str, contract_addr: str) -> celaut_pb2.Service.Api.ContractLedger:
        print("Processing payment...")
        ledger_provider = self.ledger_providers[ledger]
        if ledger_provider.add_gas(token, amount, contract_addr) != '':
            return gateway_pb2.celaut__pb2.Service.Api.ContractLedger(
                ledger = ledger,
                contract_addr = contract_addr,
                contract = CONTRACT
            )
        else:
            raise Exception('Error while adding gas')


    def payment_process_validator(self, amount: int, token: str, ledger: str, contract_addr: str, validate_token) -> bool:
        print("Validating payment...")
        ledger_provider = self.ledger_providers[ledger]
        assert contract_addr == ledger_provider.contract_addr
        return ledger_provider.validate_session(token, amount, validate_token) 


def process_payment(amount: int, token: str, ledger: str, contract_address: str) -> celaut_pb2.Service.Api.ContractLedger:
    return VyperDepositContractInterface().process_payment(amount, token, ledger, contract_address)

def payment_process_validator(amount: int, token: str, ledger: str, contract_addr: str, validate_token) -> bool:
    return VyperDepositContractInterface().payment_process_validator(amount, token, ledger, contract_addr, validate_token)