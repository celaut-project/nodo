import string
from hashlib import sha256
from time import sleep
from types import LambdaType
from typing import Dict

from protos import gateway_pb2_grpc, gateway_pb2
from src.manager.manager import PAYMENT_PROCESS_VALIDATORS, MIN_DEPOSIT_PEER, COMMUNICATION_ATTEMPTS, \
    AVAILABLE_PAYMENT_PROCESS, generate_client_id_in_other_peer, COMMUNICATION_ATTEMPTS_DELAY
from src.manager.system_cache import clients, total_deposited_on_other_peers, cache_locks
from src.utils.utils import generate_uris_by_peer_id, to_gas_amount, \
    get_ledger_and_contract_address_from_peer_id_and_ledger

import grpc
import grpcbigbuffer as grpcbf
from src.utils import logger as l

from contracts.vyper_gas_deposit_contract import interface as vyper_gdc


PAYMENT_PROCESS_VALIDATORS: Dict[bytes, LambdaType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.payment_process_validator}  # contract_hash:  lambda peer_id, tx_id, amount -> bool,
AVAILABLE_PAYMENT_PROCESS: Dict[bytes, LambdaType] = {
    vyper_gdc.CONTRACT_HASH: vyper_gdc.process_payment}  # contract_hash:   lambda amount, peer_id -> tx_id,

def __peer_payment_process(peer_id: str, amount: int) -> bool:
    deposit_token: str = generate_client_id_in_other_peer(peer_id=peer_id)
    l.LOGGER('Peer payment process to peer ' + peer_id + ' with client ' + deposit_token + ' of ' + str(amount))
    for contract_hash, process_payment in AVAILABLE_PAYMENT_PROCESS.items():  # check if the payment process is compatible with this peer.
        try:
            ledger, contract_address = get_ledger_and_contract_address_from_peer_id_and_ledger(
                contract_hash=contract_hash, peer_id=peer_id)
            l.LOGGER('Peer payment process:   Ledger: ' + str(ledger) + ' Contract address: ' + str(contract_address))
            contract_ledger = process_payment(
                amount=amount,
                token=deposit_token,
                ledger=ledger,
                contract_address=contract_address
            )
            l.LOGGER('Peer payment process: payment process executed. Ledger: ' + str(
                contract_ledger.ledger) + ' Contract address: ' + str(contract_ledger.contract_addr))
            attempt = 0
            while True:
                try:
                    next(grpcbf.client_grpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer_id=peer_id))
                            )
                        ).Payable,
                        partitions_message_mode_parser=True,
                        input=gateway_pb2.Payment(
                            gas_amount=to_gas_amount(amount),
                            deposit_token=deposit_token,
                            contract_ledger=contract_ledger,
                        )
                    )
                    )
                    break
                except Exception as e:
                    attempt += 1
                    if attempt >= COMMUNICATION_ATTEMPTS:
                        l.LOGGER('Peer payment communication process:   Failed. ' + str(e))
                        # TODO subtract node reputation
                        return False
                    sleep(COMMUNICATION_ATTEMPTS_DELAY)

            l.LOGGER('Peer payment process to ' + peer_id + ' of ' + str(amount) + ' communicated.')
        except Exception as e:
            l.LOGGER('Peer payment process error: ' + str(e))
            return False
        return True
    return False


def __increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
    l.LOGGER('Increase deposit on peer ' + peer_id + ' by ' + str(amount))
    if __peer_payment_process(peer_id=peer_id, amount=amount):  # process the payment on the peer.
        with cache_locks.lock(peer_id):
            total_deposited_on_other_peers[peer_id] = total_deposited_on_other_peers[
                                                          peer_id] + amount if peer_id in total_deposited_on_other_peers else amount
        return True
    else:
        if peer_id not in total_deposited_on_other_peers:
            with cache_locks.lock(peer_id):
                total_deposited_on_other_peers[peer_id] = 0
        return False


def increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
    try:
        return __increase_deposit_on_peer(peer_id=peer_id, amount=amount + MIN_DEPOSIT_PEER)
    except Exception as e:
        l.LOGGER('Manager error: ' + str(e))
        return False


def __check_payment_process(amount: int, ledger: str, token: str, contract: bytes, contract_addr: string) -> bool:
    l.LOGGER('Check payment process to ' + token + ' of ' + str(amount))
    if token not in clients:
        l.LOGGER('Client ' + token + ' is not in ' + str(clients))
        return False
    return PAYMENT_PROCESS_VALIDATORS[sha256(contract).digest()](amount, token, ledger, contract_addr,
                                                                 validate_token=lambda token: token in clients)


def init_contract_interfaces():
    vyper_gdc.VyperDepositContractInterface()

