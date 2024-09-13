import string
from hashlib import sha3_256
from uuid import uuid4
from time import sleep
import grpc
from grpcbigbuffer import client as grpcbf
from src.payment_system.ledger_balancer import ledger_balancer

from src.payment_system.contracts.envs import AVAILABLE_PAYMENT_PROCESS, PAYMENT_PROCESS_VALIDATORS

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

deposit_tokens = {}  # Provisional Dict[deposit_token, client_id]
def generate_deposit_token(client_id: str) -> str:
    _l.LOGGER("Generate deposit token.")
    deposit_token = str(uuid4())
    deposit_tokens[deposit_token] = client_id
    _l.LOGGER("Deposit token generated.")
    return deposit_token

def __peer_payment_process(peer_id: str, amount: int) -> bool:
    client_id: str = get_client_id_on_other_peer(peer_id=peer_id)
    if not client_id:
        _l.LOGGER("No client available.")
        return False

    _l.LOGGER(f"Generate deposit token on the peer {peer_id} with client {client_id}")
    # Get the token for identify the deposit with that client.
    deposit_token_msg: str = next(grpcbf.client_grpc(
        method=gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(
                    next(generate_uris_by_peer_id(peer_id=peer_id), None)
                )
            ).GenerateDepositToken,
        partitions_message_mode_parser=True,
        input=gateway_pb2.Client(client_id=client_id),
        indices_parser=gateway_pb2.TokenMessage
    ), None)
    _l.LOGGER(f"Deposit token msg -> {deposit_token_msg}")
    deposit_token = deposit_token_msg.token

    if not deposit_token:
        _l.LOGGER("No deposit token available.")
        return False

    _l.LOGGER(f'Peer payment process to peer {peer_id} and deposit token {deposit_token} of {amount}')
    # Try to make the payment on any platform.
    for contract_hash, process_payment in AVAILABLE_PAYMENT_PROCESS.items():
        # check if the payment process is compatible with this peer.
        try:
            for contract_address, ledger in ledger_balancer(
                    ledger_generator=get_peer_contract_instances(
                        contract_hash=contract_hash,
                        peer_id=peer_id
                    )
            ):
                _l.LOGGER(f'Peer payment process: Desposit token: {deposit_token}. Ledger: {ledger}. Contract address: contract_address')
                contract_ledger = process_payment(
                    amount=amount,
                    token=deposit_token,
                    ledger=ledger,
                    contract_address=contract_address
                )
                _l.LOGGER(f'Peer payment process: payment process executed. Deposit token {deposit_token}')
                attempt = 0
                while True:
                    try:
                        next(grpcbf.client_grpc(
                            method=gateway_pb2_grpc.GatewayStub(
                                grpc.insecure_channel(next(
                                    generate_uris_by_peer_id(peer_id=peer_id),
                                    None)
                                )
                            ).Payable,
                            partitions_message_mode_parser=True,
                            input=gateway_pb2.Payment(
                                gas_amount=to_gas_amount(amount),
                                deposit_token=deposit_token,
                                contract_ledger=contract_ledger,
                            )
                        ), None)
                        _l.LOGGER('Peer payment process to ' + peer_id + ' of ' + str(amount) + ' communicated.')
                        break
                    except Exception as e:
                        attempt += 1
                        if attempt >= COMMUNICATION_ATTEMPTS:
                            _l.LOGGER('Peer payment communication process:   Failed. ' + str(e))
                            # TODO subtract node reputation
                            return False
                        sleep(COMMUNICATION_ATTEMPTS_DELAY)
            # TODO, aqui hay que controlar el caso en que no tengamos ningun contrato disponible para ese par.
            #  porque ahora estamos diciendo que está ok. Pero en realidad no hemos hecho nada
            #  y va a entrar en loop todo el tiempo o reducirá su reputación ....
        except Exception as e:
            _l.LOGGER('Peer payment process error: ' + str(e))
            return False
        return True
    return False


def __increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
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


def increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
    try:
        return __increase_deposit_on_peer(peer_id=peer_id, amount=amount + MIN_DEPOSIT_PEER)
    except Exception as e:
        _l.LOGGER('Manager error: ' + str(e))
        return False


def validate_payment_process(amount: int, ledger: str, contract: bytes, contract_addr: str, token: str) -> bool:
    return __check_payment_process(
        amount=amount, ledger=ledger, token=token,
        contract=contract, contract_addr=contract_addr
    ) and increase_local_gas_for_client(client_id=token, amount=amount)  # TODO allow for containers too.


def __check_payment_process(amount: int, ledger: str, token: str, contract: bytes, contract_addr: string) -> bool:
    _l.LOGGER('Check payment process to ' + token + ' of ' + str(amount))
    if token not in deposit_tokens:
        _l.LOGGER(f"No token {token} in pending deposit_tokens")
        return False

    client_id = deposit_tokens[token]
    if not sc.client_exists(client_id=client_id):
        _l.LOGGER(f"Client id {client_id} not in clients.")
        return False

    _validator = PAYMENT_PROCESS_VALIDATORS[sha3_256(contract).hexdigest()]
    return _validator(amount, token, ledger, contract_addr, validate_token=lambda t: True)


def init_contract_interfaces():
    pass
