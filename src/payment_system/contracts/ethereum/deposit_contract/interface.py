import os

from src.utils.singleton import Singleton

from threading import Thread

import json
from multiprocessing import Lock

from time import sleep, time
from typing import Dict
from web3 import Web3, exceptions
from hashlib import sha3_256, sha256

from protos import celaut_pb2, gateway_pb2
from src.utils.logger import LOGGER
from src.payment_system.contracts.ethereum.utils import transact, w3_generator_factory, \
    catch_event
from src.database.access_functions.ledgers import get_ledger_and_contract_addr_from_contract, NonUsedLedgerException, \
    get_private_key_from_ledger
from src.utils.env import EnvManager

env_manager = EnvManager()

MAIN_DIR = env_manager.get_env("MAIN_DIR")

DIR = f"{MAIN_DIR}/src/payment_system/contracts/ethereum/deposit_contract"
CONTRACT: bytes = open(os.path.join(DIR, 'bytecode'), 'rb').read()
CONTRACT_HASH: str = sha3_256(CONTRACT).hexdigest()

# Vyper gas deposit contract, used to deposit gas to the contract. Inherent from the ledger and contract id.

gas_to_contract = lambda amount, parity_factor: int(amount / 10 ** parity_factor)


class LedgerContractInterface:

    def __init__(self, w3_generator, contract_addr, priv):
        LOGGER(f"{int(time())} EVM gas deposit contract interface init for {str(contract_addr)}.")
        self.w3: Web3 = next(w3_generator)
        self.contract_addr: str = contract_addr


        # TODO debe de ir en una clase para el Ledger, sin un contrato concreto.
        self.priv = priv
        self.pub = self.w3.eth.account.from_key(priv).address
        self.transaction_lock = Lock()  # TODO debería de ser un Lock global para esa cuenta, no para esa cuenta en un contrato concreto.
        self.last_nonce: int = 0
        self.nonce_count: int = 0

        self.generate_contract = lambda addr: self.w3.eth.contract(
            address=Web3.to_checksum_address(addr),
            abi=json.load(open(DIR + 'abi.json')),
            bytecode=open(DIR + 'bytecode', 'rb').read()
        )
        self.contract = self.generate_contract(addr=contract_addr)

        self.payment_sessions: Dict[bytes, int] = {}
        self.sessions_lock = Lock()

        # TODO this variables depends on the ledger, so they should be moved on the contracts table OR be dynamically updated.
        self.poll_interval: int = 2
        self.poll_iterations: int = 60
        self.poll_init_delay: int = 20
        self.wait_mint_timeout: int = 120
        self.wait_mint_poll_latency: float = 0.1

        self.contract_to_gas = lambda amount: amount * 10 ** self.contract.functions.get_parity_factor().call()

        Thread(target=self.catch_event_thread, args=(contract_addr,)).start()

    def get_nonce(self) -> int:  # TODO get_nonce debería de estar ethereum/utils ???
        with self.transaction_lock:
            last_nonce = self.w3.eth.get_transaction_count(self.pub)
            if last_nonce == self.last_nonce:
                self.nonce_count += 1
            else:
                self.nonce_count = 0
                self.last_nonce = last_nonce
            LOGGER(f"          {self.pub} 's   nonce of {str(self.nonce_count + last_nonce)}")
            return self.nonce_count + last_nonce

    # Update Session Event.
    def catch_event_thread(self, contract_addr):
        catch_event(
            contract_address=Web3.to_checksum_address(contract_addr),
            w3=self.w3,
            contract=self.contract,
            event_name='NewSession',
            init_delay=self.poll_init_delay,
            opt=lambda args: self.__new_session(
                token=args['token'],
                amount=args['amount']
            ),
            poll_interval=self.poll_interval,
        )

    def __new_session(self, token, amount):
        amount = self.contract_to_gas(amount)
        with self.sessions_lock:
            if token not in self.payment_sessions:
                self.payment_sessions[token] = amount
            else:
                self.payment_sessions[token] += amount
            LOGGER(f'\n {int(time())} New session: {token}  {amount} \n')

    def validate_payment_session(self, token: str, amount: int, validate_token=None) -> bool:
        token_encoded: bytes = sha256(token.encode('utf-8')).digest()
        for i in range(self.poll_iterations):
            if token_encoded in self.payment_sessions and self.payment_sessions[token_encoded] >= amount and \
                    (not validate_token or validate_token(token)):
                with self.sessions_lock:
                    self.payment_sessions[token_encoded] -= amount
                return True
            else:
                sleep(self.poll_interval)

        LOGGER(f"Error: El token {token} no se encuentra en las sesiones existentes ({self.payment_sessions}).\n"
               if token_encoded not in self.payment_sessions
               else f"Error: El token {token} no tiene suficiente gas "
                    f"disponible ({self.payment_sessions[token_encoded]})."
                    f" Se necesitan {amount - self.payment_sessions[token_encoded]} de {amount} gas.\n"
        if self.payment_sessions[token_encoded] < amount
        else f"Error: El token {token} no es válido.\n"
        if not validate_token(token)
        else f"Error: ¡Token {token} validado correctamente! Aunque algo ocurrió.\n")
        return False

    def add_gas(self, token: str, amount: int, contract_addr: str) -> str:
        contract = self.generate_contract(addr=contract_addr)
        gas = 2000000
        while True:
            nonce = self.get_nonce()
            try:
                return transact(
                    w3=self.w3,
                    method=contract.functions.add_gas(
                        sha256(token.encode('utf-8')).digest(),
                    ),
                    priv=self.priv,
                    nonce=nonce,
                    value=gas_to_contract(amount, contract.functions.get_parity_factor().call()),
                    gas=gas,
                    timeout=self.wait_mint_timeout,
                    poll_latency=self.wait_mint_poll_latency,
                )
            except exceptions.TimeExhausted:
                LOGGER(f'Timeout while adding gas for token:  {token} with {gas} gas.\n')
                gas += gas
                continue
                # return ''
            except Exception as e:
                if str(e) == "{'code': -32000, 'message': 'already known'}":
                    LOGGER(f'Transaction already known: {token} \n')
                    continue
                else:
                    LOGGER(f'Error {str(e)} while adding gas for token: {token}\n')
                    return ''


# Singleton class
class VyperDepositContractInterface(metaclass=Singleton):

    def __init__(self):
        self.ledger_providers: Dict[str: LedgerContractInterface] = {}
        for contract_address, ledger in get_ledger_and_contract_addr_from_contract(contract_hash=CONTRACT_HASH):
            try:
                self.ledger_providers[ledger] = LedgerContractInterface(
                    w3_generator=w3_generator_factory(ledger=ledger),
                    contract_addr=contract_address,
                    priv=get_private_key_from_ledger(ledger)
                )
            except NonUsedLedgerException:
                pass

    # TODO si necesitas añadir un nuevo ledger, deberás reiniciar el nodo, a no ser que se implemente un método
    #  set_ledger_on_interface()

    def process_payment(self, amount: int, token: str, ledger: str,
                        contract_addr: str) -> celaut_pb2.Service.Api.ContractLedger:
        LOGGER("Processing payment...")
        ledger_provider = self.ledger_providers[ledger]
        if ledger_provider.add_gas(token, amount, contract_addr) != '':
            return gateway_pb2.celaut__pb2.Service.Api.ContractLedger(
                ledger=ledger,
                contract_addr=contract_addr,
                contract=CONTRACT
            )
        else:
            raise Exception('Error while adding gas')

    def payment_process_validator(self, amount: int, token: str, ledger: str, contract_addr: str,
                                  validate_token) -> bool:
        LOGGER("Validating payment...")
        ledger_provider = self.ledger_providers[ledger]
        assert contract_addr == ledger_provider.contract_addr
        return ledger_provider.validate_payment_session(token, amount, validate_token)


def process_payment(amount: int, token: str, ledger: str,
                    contract_address: str) -> celaut_pb2.Service.Api.ContractLedger:
    return VyperDepositContractInterface().process_payment(amount, token, ledger, contract_address)


def payment_process_validator(amount: int, token: str, ledger: str, contract_addr: str, validate_token) -> bool:
    return VyperDepositContractInterface().payment_process_validator(amount, token, ledger, contract_addr,
                                                                     validate_token)
