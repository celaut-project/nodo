import string
from hashlib import sha3_256
from uuid import uuid4
from src.payment_system.contracts import simulator
from time import sleep
import grpc
from grpcbigbuffer import client as grpcbf
from src.payment_system.ledger_balancer import ledger_balancer

from src.payment_system.contracts.envs import AVAILABLE_PAYMENT_PROCESS, INIT_INTERFACES, PAYMENT_PROCESS_VALIDATORS, DEMOS

from protos import gateway_pb2_grpc, gateway_pb2

from src.manager.manager import get_client_id_on_other_peer, increase_local_gas_for_client
from src.database.sql_connection import SQLConnection

from src.utils import logger as _l
from src.utils.utils import to_gas_amount, generate_uris_by_peer_id
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.utils.env import EnvManager

env_manager = EnvManager()

COMMUNICATION_ATTEMPTS = env_manager.get_env("COMMUNICATION_ATTEMPTS")
COMMUNICATION_ATTEMPTS_DELAY = env_manager.get_env("COMMUNICATION_ATTEMPTS_DELAY")
MIN_DEPOSIT_PEER = env_manager.get_env("MIN_DEPOSIT_PEER")

sc = SQLConnection()

def generate_deposit_token(client_id: str) -> str:
    _l.LOGGER("Generate deposit token.")
    deposit_token = sc.add_deposit_token(client_id=client_id, status=['pending'])
    _l.LOGGER(f"Deposit token {deposit_token} generated.")
    return deposit_token


# Helper function to create the gRPC stub and get URIs
def __get_grpc_stub(peer_id):
    uri = next(generate_uris_by_peer_id(peer_id=peer_id), None)
    if uri is None:
        return None
    return gateway_pb2_grpc.GatewayStub(grpc.insecure_channel(uri))


def __peer_payment_process(peer_id: str, amount: int) -> bool:
    client_id: str = get_client_id_on_other_peer(peer_id=peer_id)
    if not client_id:
        _l.LOGGER("No client available.")
        return False

    _l.LOGGER(f"Generate deposit token on the peer {peer_id} with client {client_id}")

    # Generate the deposit token
    grpc_stub = __get_grpc_stub(peer_id)
    if not grpc_stub:
        _l.LOGGER("Failed to generate gRPC stub.")
        return False

    try:
        deposit_token = next(grpcbf.client_grpc(
            method=grpc_stub.GenerateDepositToken,
            partitions_message_mode_parser=True,
            input=gateway_pb2.Client(client_id=client_id),
            indices_parser=gateway_pb2.TokenMessage
        ), None).token
    except Exception as e:
        _l.LOGGER(f"Error generating deposit token: {str(e)}")
        return False

    if not deposit_token:
        _l.LOGGER("No deposit token available.")
        return False

    # Attempt payment processing for each available payment process
    for contract_hash, process_payment in AVAILABLE_PAYMENT_PROCESS.items():
        try:
            # Get all available ledgers for this peer and contract
            ledgers = get_peer_contract_instances(contract_hash, peer_id) if contract_hash not in DEMOS else [("", "")]
            for contract_address, ledger in ledger_balancer(ledger_generator=ledgers):
                _l.LOGGER(f"Processing payment: Deposit token: {deposit_token}. Ledger: {ledger}. Contract address: {contract_address}")

                # Process the payment
                contract_ledger = process_payment(
                    amount=amount,
                    deposit_token=deposit_token,
                    ledger=ledger,
                    contract_address=contract_address
                )
                _l.LOGGER(f"Payment processed. Deposit token: {deposit_token}")

                # Handle communication attempts to peer
                if __attempt_payment_communication(peer_id, amount, deposit_token, contract_ledger):
                    return True
                _l.LOGGER(f"Failed to communicate payment for contract {contract_hash}")
                # TODO Should decrease reputation on peer or payment network.

            _l.LOGGER(f"No compatible contract found for {contract_hash}")
        except Exception as e:
            _l.LOGGER(f"Error processing payment for contract {contract_hash}: {str(e)}")
            continue  # Continue to next contract hash
        return True

    _l.LOGGER("No available payment process.")
    return False


# Helper function for payment communication retries
def __attempt_payment_communication(peer_id: str, amount: int, deposit_token: str, contract_ledger: gateway_pb2.celaut__pb2.Service.Api.ContractLedger) -> bool:
    attempt = 0
    while attempt < COMMUNICATION_ATTEMPTS:
        try:
            grpc_stub = __get_grpc_stub(peer_id)
            if not grpc_stub:
                _l.LOGGER(f"Failed to get gRPC stub for peer {peer_id}")
                return False

            next(grpcbf.client_grpc(
                method=grpc_stub.Payable,
                partitions_message_mode_parser=True,
                input=gateway_pb2.Payment(
                    gas_amount=to_gas_amount(amount),
                    deposit_token=deposit_token,
                    contract_ledger=contract_ledger,
                )
            ), None)

            _l.LOGGER(f"Payment of {amount} to {peer_id} communicated successfully.")
            return True
        except Exception as e:
            attempt += 1
            _l.LOGGER(f"Communication attempt {attempt} failed: {str(e)}")
            if attempt >= COMMUNICATION_ATTEMPTS:
                _l.LOGGER(f"Max communication attempts reached for {peer_id}.")
                return False
            sleep(COMMUNICATION_ATTEMPTS_DELAY)
    return False


def increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
    if amount < MIN_DEPOSIT_PEER: amount = MIN_DEPOSIT_PEER
    _l.LOGGER('Increase deposit on peer ' + peer_id + ' by ' + str(amount))
    try:
        if __peer_payment_process(peer_id=peer_id, amount=amount):
            if sc.add_gas_to_peer(peer_id=peer_id, gas=amount):
                return True
            else:
                _l.LOGGER(f'Failed to add gas to peer {peer_id}')
                return False
        else:
            return False
    except Exception as e:
        _l.LOGGER(f'Error increasing deposit on peer {peer_id}: {e}')
        return False


def validate_payment_process(amount: int, ledger: str, contract: bytes, contract_addr: str, token: str) -> bool:
    if not sc.deposit_token_exists(token_id=token, status='pending'):
        raise Exception(f"Deposit token {token} doesn't exists.")
    try:
        _r = __check_payment_process(
            amount=amount, ledger=ledger, token=token,
            contract=contract, contract_addr=contract_addr
        ) and increase_local_gas_for_client(client_id=sc.client_id_from_deposit_token(token_id=token), amount=amount)  # TODO allow for containers too.
    except: _r = False
    if _r: sc.delete_deposit_token(token_id=token)
    return _r


def __check_payment_process(amount: int, ledger: str, token: str, contract: bytes, contract_addr: str) -> bool:
    _l.LOGGER('Check payment process to ' + token + ' of ' + str(amount))
    if not sc.deposit_token_exists(token_id=token, status='pending'):
        _l.LOGGER(f"No token {token} in pending deposit_tokens")
        return False

    client_id = sc.client_id_from_deposit_token(token_id=token)
    if not sc.client_exists(client_id=client_id):
        _l.LOGGER(f"Client id {client_id} not in clients.")
        return False

    _validator = PAYMENT_PROCESS_VALIDATORS[sha3_256(contract).hexdigest()]
    return _validator(amount, token, ledger, contract_addr)


def init_interfaces():
    _l.LOGGER("Init payment interfaces")
    # Initialize all payment interfaces if they are callable
    for key, _init in INIT_INTERFACES.items():
        _l.LOGGER(f"Init {key[:6]}... payment interface.")
        if callable(_init):
            _init()  # Execute the init function
        else:
            _l.LOGGER(f"Warning: {_init} is not callable.")
