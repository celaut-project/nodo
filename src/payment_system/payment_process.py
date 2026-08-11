from hashlib import sha3_256
from threading import Thread
from time import sleep
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional
from contextlib import nullcontext
import grpc
from bee_rpc import client as bee
from src.payment_system.exceptions import DoubleSpendingAttempt
from src.payment_system.ledger_balancer import ledger_balancer

from protos import celaut_pb2_grpc, celaut_pb2

from src.database.sql_connection import SQLConnection

from src.utils import logger as _l
from src.utils.utils import to_amount, generate_uris_by_peer_id
from src.utils.monetary import format_mu
from src.database.access_functions.ledgers import get_peer_contract_instances
from src.utils.config import ConfigManager
from src.utils.java_dependency import JavaDependencyMissing, log_java_dependency_warning

env_manager = ConfigManager()

COMMUNICATION_ATTEMPTS = int(env_manager.get("COMMUNICATION_ATTEMPTS"))
COMMUNICATION_ATTEMPTS_DELAY = int(env_manager.get("COMMUNICATION_ATTEMPTS_DELAY"))
PAYMENT_MANAGER_ITERATION_TIME = int(env_manager.get("ledgers.ergo.payments.PAYMENT_MANAGER_ITERATION_TIME"))

sc = SQLConnection()
deposit_generation_locked = False

auxiliar_script_reputation = {}
auxiliar_script_reputation_lock = Lock()


def _payment_envs():
    from src.payment_system.contracts import envs
    return envs


def _reputation_interface():
    from src.reputation_system import interface
    return interface


def _manager_module():
    from src.manager import manager
    return manager

def generate_deposit_token(client_id: str) -> str:
    if deposit_generation_locked:
        raise Exception("Deposit generation locked. Try later.")
    _l.LOGGER("Generate deposit token.")
    deposit_token = sc.add_deposit_token(client_id=client_id, status='pending')
    _l.LOGGER(f"Deposit token {deposit_token} generated.")
    return deposit_token


# Helper function to create the gRPC stub and get URIs
def __get_grpc_stub(peer_id):
    uri = next(generate_uris_by_peer_id(peer_id=peer_id), None)
    if uri is None:
        return None
    return celaut_pb2_grpc.GatewayStub(grpc.insecure_channel(uri))


def __obtain_deposit_token(peer_id) -> Optional[str]:
    client_id: str = _manager_module().get_client_id_on_other_peer(peer_id=peer_id)
    if not client_id:
        _l.LOGGER("No client available.")
        return

    _l.LOGGER(f"Generate deposit token on the peer {peer_id} with client {client_id}")

    # Generate the deposit token
    grpc_stub = __get_grpc_stub(peer_id)
    if not grpc_stub:
        _l.LOGGER("Failed to generate gRPC stub.")
        return

    try:
        return next(bee.client_grpc(
            method=grpc_stub.GenerateDepositToken,
            partitions_message_mode_parser=True,
            input=celaut_pb2.Client(client_id=client_id),  # type: ignore
            indices_parser=celaut_pb2.TokenMessage  # type: ignore
        ), None).token  # type: ignore
    except Exception as e:
        _l.LOGGER(f"Error generating deposit token: {str(e)}")
        return

def __peer_payment_process(peer_id: str, amount: int, on_transaction_url=None) -> bool:
    payment_envs = _payment_envs()
    deposit_token = None

    # Attempt payment processing for each available payment process
    for contract_hash, process_payment in payment_envs.available_payment_process().items():
        
        # In the case where we have different payment methods for the same ledger, ex: other payment method on Ergo, we should reorganize the envs dictionaries to avoid check sender balances multiple times.
        
        check_balance = payment_envs.check_sender_balances()[contract_hash]
        if not check_balance(amount):
            _l.LOGGER(f"Insufficient balance for contract {contract_hash[:6]}.")
            continue
        
        if not deposit_token:
            deposit_token = __obtain_deposit_token(peer_id=peer_id)
            if not deposit_token:
                _l.LOGGER("No deposit token available.")
                return False
            else:
                _l.LOGGER("Deposit token obtained.")
        
        try:
            # Get all available ledgers for this peer and contract
            
            scripts = get_peer_contract_instances(contract_hash, peer_id)
            ledgers = ledger_balancer(ledger_generator=scripts) if contract_hash not in payment_envs.DEMOS else [("", "")]
            
            for script, ledger in ledgers:
                
                with auxiliar_script_reputation_lock:
                    # Check if contract address is in the auxiliar dictionary        TODO use reputation instead.
                    if script in auxiliar_script_reputation:
                        if auxiliar_script_reputation[script] > datetime.now():
                            continue
                        else:
                            del auxiliar_script_reputation[script]

                _l.LOGGER(f"Processing payment: Deposit token: {deposit_token}. Ledger: {ledger}. Contract address: {script}")

                # Process the payment
                try:
                    report_url = getattr(payment_envs, "transaction_url_reporting", None)
                    reporting_context = (
                        report_url(on_transaction_url)
                        if callable(report_url)
                        else nullcontext()
                    )
                    with reporting_context:
                        contract_ledger = process_payment(
                            amount=amount,
                            deposit_token=deposit_token,
                            ledger=ledger,
                            script=script
                        )
                    _l.LOGGER(f"Payment processed. Deposit token: {deposit_token}")
                    # if token_idess and ledger:
                        # update_reputation(=token_idess, amount=10)  # TODO On envs.
                        # update_reputation(=ledger, amount=1)  # TODO On envs.
                except DoubleSpendingAttempt as e:
                    _l.LOGGER(str(e))
                    # Internally, the exception updates the wait time to retry the ledger. 
                    # It is not necessary to update its reputation at this point.
                    continue
                except Exception as e:
                    _l.LOGGER(f"Error processing payment for contract {contract_hash}: {str(e)}")
                    
                    # TODO
                    # In case of failure, we need to handle attempts to retry x times
                    # and if it still fails, leave it until after x time or something similar.
                    # This is auxiliary because ideally, it would be based on its reputation.

                    if script:
                        with auxiliar_script_reputation_lock:  # TODO use reputation instead.
                            if script not in auxiliar_script_reputation:
                                auxiliar_script_reputation[script] = datetime.now()
                            auxiliar_script_reputation[script] += timedelta(seconds=600)  # Adds 10 minutes.

                    # if token_idess and ledger:
                        # update_reputation(=token_idess, amount=-100)  # TODO On envs.
                        # update_reputation(=ledger, amount=-10)  # TODO On envs.
                    continue


                # Handle communication attempts to peer
                if __attempt_payment_communication(peer_id, amount, deposit_token, contract_ledger):
                    _reputation_interface().update_peer_reputation(peer_id=peer_id, amount=10)  # TODO On envs.
                    return True
                else:
                    _l.LOGGER(f"Failed to communicate payment for contract {contract_hash}")
                    _reputation_interface().update_peer_reputation(peer_id=peer_id, amount=-100)  # TODO On envs.

            _l.LOGGER(f"No compatible contract found for {contract_hash}")
        except JavaDependencyMissing:
            log_java_dependency_warning(_l.LOGGER, feature="Ergo payments or reputation")
            return False
        except Exception as e:
            _l.LOGGER(f"Unhandled exception on payment process for {contract_hash}: {e}")

    _l.LOGGER("No available payment process.")
    return False


# Helper function for payment communication retries
def __attempt_payment_communication(peer_id: str, amount: int, deposit_token: str, contract_ledger: celaut_pb2.Contract) -> bool:
    attempt = 0
    while attempt < COMMUNICATION_ATTEMPTS:
        try:
            grpc_stub = __get_grpc_stub(peer_id)
            if not grpc_stub:
                _l.LOGGER(f"Failed to get gRPC stub for peer {peer_id}")
                return False

            next(bee.client_grpc(
                method=grpc_stub.Payable,
                partitions_message_mode_parser=True,
                input=celaut_pb2.Payment(
                    amount=to_amount(amount),
                    deposit_token=deposit_token,
                    contract=contract_ledger,
                )
            ), None)

            _l.LOGGER(f"Payment of {amount} to {peer_id} communicated successfully.")
            return True
        except Exception as e:
            _reputation_interface().update_vmachine_reputation(vmachine_id=peer_id, amount=-1)  # TODO On envs.
            attempt += 1
            _l.LOGGER(f"Communication attempt {attempt} failed: {str(e)}")
            if attempt >= COMMUNICATION_ATTEMPTS:
                _l.LOGGER(f"Max communication attempts reached for {peer_id}.")
                return False
            sleep(COMMUNICATION_ATTEMPTS_DELAY)
    return False


def increase_deposit_on_peer(peer_id: str, amount: int, on_transaction_url=None) -> bool:
    # Never send less than a full deposit. That floor is derived from what the ledger
    # can actually settle -- a smaller transfer would spend more on its own fee than it
    # delivers -- so it is computed, not configured. See src/payment_system/deposits.py.
    from src.payment_system.deposits import full_deposit_mu

    amount = max(int(amount), full_deposit_mu())

    _l.LOGGER(f"Increase deposit on peer {peer_id} by {format_mu(amount)}")
    try:
        if __peer_payment_process(
            peer_id=peer_id,
            amount=amount,
            on_transaction_url=on_transaction_url,
        ):
            if sc.add_balance_to_peer(peer_id=peer_id, balance_mu=amount):
                return True
            else:
                _l.LOGGER(f'Failed to update the balance for peer {peer_id} on DB')
                return False
        else:
            _l.LOGGER(f'Failed to add balance to peer {peer_id}')
            return False
    except Exception as e:
        _l.LOGGER(f'Error increasing deposit on peer {peer_id}: {e}')
        return False


def validate_payment_process(amount: int, ledger: celaut_pb2.Contract.Ledger, contract: bytes, script: bytes, token: str) -> bool:
    if not sc.deposit_token_exists(token_id=token, status='pending'):
        raise Exception(f"Deposit token {token} doesn't exists.")
    try:
        _r = __check_payment_process(
            amount=amount, ledger=ledger, token=token,
            contract=contract, script=script
        ) and _manager_module().increase_local_balance_for_client(client_id=sc.client_id_from_deposit_token(token_id=token), amount_mu=amount)  # TODO allow for containers too.
    except: _r = False
    sc.update_deposit_token(token_id=token, status="payed" if _r else "rejected")
    _l.LOGGER(f"Pending deposit tokens updated, there are still {len(sc.get_deposit_tokens(status='pending'))} tokens in the queue.")
    return _r


def __check_payment_process(amount: int, ledger: celaut_pb2.Contract.Ledger, token: str, contract: bytes, script: bytes) -> bool:
    _l.LOGGER('Check payment process to ' + token + ' of ' + str(amount))
    if not sc.deposit_token_exists(token_id=token, status='pending'):
        _l.LOGGER(f"No token {token} in pending deposit_tokens")
        return False

    client_id = sc.client_id_from_deposit_token(token_id=token)
    if not sc.client_exists(client_id=client_id):
        _l.LOGGER(f"Client id {client_id} not in clients.")
        return False

    _validator = _payment_envs().payment_process_validators()[sha3_256(contract).hexdigest()]
    return _validator(amount, token, ledger, script)


def __manage_interfaces():
    while True:
        sleep(PAYMENT_MANAGER_ITERATION_TIME)
        _l.LOGGER("Execute payment manager iteration.")

        deposit_generation_locked = True

        while True:
            sleep(1)
            if len(sc.get_deposit_tokens(status="pending")) == 0:
                _l.LOGGER("Any pending deposit token, now payment interfaces can be managed.")
                break

        for key, _manage in _payment_envs().manage_interfaces().items():
            if callable(_manage):
                try:
                    _manage()
                except JavaDependencyMissing:
                    log_java_dependency_warning(_l.LOGGER, feature="Ergo payments or reputation")
                except Exception as e:
                    _l.LOGGER(f"Exception on manage interface {key}. {str(e)}")
            else:
                _l.LOGGER(f"Warning: {_manage} is not callable.")

        deposit_generation_locked = False


def init_interfaces():
    Thread(target=__manage_interfaces, daemon=True).start()
    for key, _init in _payment_envs().init_interfaces().items():
        if callable(_init):
            try:
                _init()
            except JavaDependencyMissing:
                log_java_dependency_warning(_l.LOGGER, feature="Ergo payments or reputation")
            except Exception as e:
                _l.LOGGER(f"Exception on init interface {key}. {str(e)}")
        else:
            _l.LOGGER(f"Warning: {_init} is not callable.")
