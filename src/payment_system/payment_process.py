from hashlib import sha3_256
from threading import Thread
from time import monotonic, sleep
from datetime import datetime, timedelta
from threading import Lock
from dataclasses import dataclass
from typing import List, Optional, Tuple
from contextlib import nullcontext
from bee_rpc import client as bee
from src.payment_system.exceptions import DoubleSpendingAttempt
from src.payment_system.ledger_balancer import ledger_balancer

from protos import celaut_pb2_grpc, celaut_pb2

from src.database.sql_connection import SQLConnection

from src.utils import logger as _l
from src.identity.grpc_transport import peer_channel
from src.identity.tls_identity import CertificateError
from src.utils.utils import to_amount
from src.utils.monetary import format_mu
from src.database.access_functions.ledgers import get_peer_contract_instances
# Plain constants, no imports of its own, so this cannot be the edge that drags the
# reputation stack (and the JVM behind it) into the payment path -- see
# `_reputation_interface` above, which stays lazy for exactly that reason.
from src.reputation_system.reasons import Reason
from src.utils.config import ConfigManager
from src.utils.java_dependency import JavaDependencyMissing, log_java_dependency_warning

env_manager = ConfigManager()

COMMUNICATION_ATTEMPTS = int(env_manager.get("COMMUNICATION_ATTEMPTS"))
COMMUNICATION_ATTEMPTS_DELAY = int(env_manager.get("COMMUNICATION_ATTEMPTS_DELAY"))
PAYMENT_MANAGER_ITERATION_TIME = int(env_manager.get("ledgers.ergo.payments.PAYMENT_MANAGER_ITERATION_TIME"))

sc = SQLConnection()
deposit_generation_locked = False

# How long a sweep waits for in-flight deposits before giving up on this iteration.
DEPOSIT_DRAIN_TIMEOUT = 300

# How long a deposit token may sit 'pending' before it is written off. A payment takes
# seconds (submit the transaction, then call Payable), so an hour is not a deadline
# anyone meets by accident -- and a client whose payment lands after it is refused.
DEPOSIT_TOKEN_TTL = 3600

auxiliar_script_reputation = {}
auxiliar_script_reputation_lock = Lock()


@dataclass(frozen=True)
class SettlementPlan:
    """One way to pay a peer: through which contract, and for how much on each side.

    Both figures belong to *this* contract, and that is the point. They used to be
    resolved once, up front, from whichever payment system happened to be picked -- and
    then handed to a loop that was free to settle through a different one. With a single
    contract the two can never disagree. With two they can, silently: the node pays over
    Bitcoin and tells the peer a figure converted at Ergo's rate, so `Payable` claims a
    credit the transaction does not carry and the peer's validator rejects a payment
    that is already on-chain.
    """

    contract_hash: str
    #: ``None`` for a payment that settles on no chain.
    ledger_tag: Optional[str]
    #: What leaves our wallet, in our MU, after this contract's own floors.
    amount: int
    #: What the peer is told to credit, in the peer's MU, at this contract's rate.
    peer_amount: int
    is_demo: bool = False


def _payment_envs():
    from src.payment_system.contracts import envs
    return envs


def _reputation_interface():
    from src.reputation_system import interface
    return interface


def _manager_module():
    from src.manager import manager
    return manager


def _donation_accrual():
    from src.payment_system.donations import accrual
    return accrual


def _ledger_tag(ledger) -> Optional[str]:
    """The ledger's tag ("ergo"), for a row a person reads. Demo payments carry none."""
    tags = getattr(ledger, "tags", None)
    return tags[0] if tags else None


def _address_of(script) -> Optional[str]:
    """Where a payment went, in the form `contract_instance.address` stores it.

    `get_peer_contract_instances` hands back the raw propositionBytes it decoded out of
    that column's hex, so re-encoding is what keeps the two joinable. Deriving the
    base58 address instead would need the JVM, on a path that already has the money out
    the door and must not be able to fail.
    """
    if isinstance(script, bytes):
        return script.hex() or None
    return script or None


def generate_deposit_token(client_id: str) -> str:
    if not env_manager.get("client.ACCEPT_NEW_DEPOSITS", True):
        raise Exception("This node is not accepting new deposits.")
    if deposit_generation_locked:
        raise Exception("Deposit generation locked. Try later.")
    _l.LOGGER("Generate deposit token.")
    deposit_token = sc.add_deposit_token(client_id=client_id, status='pending')
    _l.LOGGER(f"Deposit token {deposit_token} generated.")
    return deposit_token


# Helper function to create the gRPC stub and get URIs
def __get_grpc_stub(peer_id):
    try:
        return celaut_pb2_grpc.GatewayStub(peer_channel(peer_id=peer_id))
    except (ConnectionError, CertificateError) as e:
        # Same contract as before -- callers treat None as "cannot reach this peer" --
        # but a certificate that does not prove `peer_id` now lands here too: this stub
        # is what `pay` sends money over, so an unverified answer is not a peer.
        _l.LOGGER(f"No verified channel to peer {peer_id}: {e}")
        return None


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

def __peer_payment_process(peer_id: str, plans: List[SettlementPlan],
                           on_transaction_url=None) -> Optional[SettlementPlan]:
    """Try each plan in turn; the first one that settles and is acknowledged wins.

    **Funding is the selection.** There is no preference policy and none is needed:
    whichever system this node has money on is the one that pays. The loop already
    skipped a contract whose wallet could not fund the amount; what is new is that the
    figures it uses are that contract's own, so the amount the peer is told always
    belongs to the contract that actually settled (#340 §4.4).

    Returns the plan that settled, not just success: the caller has to credit the peer
    the figure *that* contract converted, and with several candidates it cannot know
    which one that was. Truthy on success and ``None`` on failure, so it still reads as
    a boolean at a call site that only asks whether the money moved.
    """
    payment_envs = _payment_envs()
    deposit_token = None
    processes = payment_envs.available_payment_process()
    balances = payment_envs.check_sender_balances()

    for plan in plans:
        contract_hash = plan.contract_hash
        amount = plan.amount
        peer_amount = plan.peer_amount
        process_payment = processes.get(contract_hash)
        if process_payment is None:
            # Shared with the peer, but this node cannot settle it right now -- a
            # runtime that went away between matching and paying.
            _l.LOGGER(f"No payment process available for contract {contract_hash[:6]}.")
            continue

        check_balance = balances.get(contract_hash)
        if check_balance and not check_balance(amount):
            _l.LOGGER(
                f"Insufficient balance for contract {contract_hash[:6]}; trying the "
                "next payment system."
            )
            continue
        
        if not deposit_token:
            deposit_token = __obtain_deposit_token(peer_id=peer_id)
            if not deposit_token:
                _l.LOGGER("No deposit token available.")
                return None
            else:
                _l.LOGGER("Deposit token obtained.")
        
        try:
            # Get all available ledgers for this peer and contract
            
            scripts = get_peer_contract_instances(contract_hash, peer_id)
            ledgers = [("", "")] if plan.is_demo else ledger_balancer(ledger_generator=scripts)
            
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
                # Collects the id of the transaction the contract submits, so the payment
                # can be written down as the thing that actually happened on a chain.
                # A list because the hook is a callback and there is nothing else to
                # carry the value back through the context manager.
                submitted_tx: list = []

                try:
                    # Reported against *this* contract: with two payment systems, a
                    # hook resolved from another one would attach a transaction id to
                    # the wrong payment.
                    report_url = getattr(payment_envs, "transaction_url_reporting", None)
                    reporting_context = (
                        report_url(on_transaction_url, contract_hash=contract_hash)
                        if callable(report_url)
                        else nullcontext()
                    )
                    report_id = getattr(payment_envs, "transaction_id_reporting", None)
                    id_context = (
                        report_id(submitted_tx.append, contract_hash=contract_hash)
                        if callable(report_id)
                        else nullcontext()
                    )
                    with reporting_context, id_context:
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


                # Past this point the payment exists on the ledger, whatever the peer
                # does next, so both branches below write it down. The failing one is
                # the row that matters most: money left this wallet and no balance
                # arrived, and until now that left no trace an operator could read.
                def record(status: str):
                    sc.record_payment(
                        direction='out',
                        status=status,
                        amount_mu=amount,
                        tx_id=submitted_tx[-1] if submitted_tx else None,
                        peer_id=peer_id,
                        deposit_token=deposit_token,
                        ledger=_ledger_tag(ledger) or plan.ledger_tag,
                        contract_hash=contract_hash,
                        address=_address_of(script),
                    )

                # Handle communication attempts to peer
                if __attempt_payment_communication(peer_id, peer_amount, deposit_token, contract_ledger):
                    record('communicated')
                    _reputation_interface().update_peer_reputation(
                        peer_id=peer_id, amount=10,  # TODO On envs.
                        reason=Reason.PAYMENT_COMMUNICATED
                    )
                    return plan
                else:
                    _l.LOGGER(f"Failed to communicate payment for contract {contract_hash}")
                    record('unacknowledged')
                    _reputation_interface().update_peer_reputation(
                        peer_id=peer_id, amount=-100,  # TODO On envs.
                        reason=Reason.PAYMENT_UNACKNOWLEDGED
                    )

            _l.LOGGER(f"No compatible contract found for {contract_hash}")
        except JavaDependencyMissing:
            log_java_dependency_warning(_l.LOGGER, feature="Ergo payments or reputation")
            # Not a bail-out: the JVM is missing for every Ergo-backed contract, but
            # another ledger's plan is still worth trying.
            continue
        except Exception as e:
            _l.LOGGER(f"Unhandled exception on payment process for {contract_hash}: {e}")

    _l.LOGGER("No payment system settled this deposit.")
    return None


# Helper function for payment communication retries
def __attempt_payment_communication(peer_id: str, peer_amount: int, deposit_token: str, contract_ledger: celaut_pb2.Contract) -> bool:
    """``peer_amount`` is in the *peer's* MU: MU is an internal unit, so the figure
    a peer is told has to be on its own scale (see ``__deposit_amounts``).
    """
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
                    amount=to_amount(peer_amount),
                    deposit_token=deposit_token,
                    contract=contract_ledger,
                )
            ), None)

            _l.LOGGER(f"Payment of {peer_amount} (peer MU) to {peer_id} communicated successfully.")
            return True
        except Exception as e:
            # A peer that will not take our `Payable` call is the peer failing us, so
            # this is a peer penalty and is written as one. It used to be handed to
            # `update_vmachine_reputation` with a peer id in the vmachine argument,
            # which meant it landed nowhere at all.
            _reputation_interface().update_peer_reputation(
                peer_id=peer_id, amount=-1,  # TODO On envs.
                reason=Reason.PAYMENT_CALL_FAILED
            )
            attempt += 1
            _l.LOGGER(f"Communication attempt {attempt} failed: {str(e)}")
            if attempt >= COMMUNICATION_ATTEMPTS:
                _l.LOGGER(f"Max communication attempts reached for {peer_id}.")
                return False
            sleep(COMMUNICATION_ATTEMPTS_DELAY)
    return False


def __settlement_plans(peer_id: str, amount: int, *, floor: bool) -> Tuple[List[SettlementPlan], List[str]]:
    """One :class:`SettlementPlan` per shared payment system, and why any were dropped.

    In the order the payer should try them (see `matching_payment_systems`), each with
    its own floors applied and its own rate used for the peer's figure. Every part of
    that is per system, because every part of it differs per chain: a fee, a minimum
    output, and how many of the peer's MU one unit buys.

    ``floor`` is the same flag that already separates the two callers. The automatic
    refill named no figure, so it is raised -- to a full deposit for that system, and
    then to that ledger's minimum output if it is still below it. An operator who typed
    an amount gets that amount or a reason, never a larger payment they did not ask for.

    The peer's figure rounds down: its validator checks the payment is worth at least
    the MU it is asked to credit, and claiming one MU more than the transaction carries
    would have it reject a payment already on-chain.

    Returns an empty list rather than raising when no system can carry the amount; the
    reasons come back alongside so a caller can tell an operator which floor stopped it.
    Raises only when there is no shared system at all.
    """
    from src.payment_system.deposits import full_deposit_mu
    from src.payment_system.mu_conversion import convert_mu, matching_payment_systems

    payment_envs = _payment_envs()
    try:
        systems = matching_payment_systems(peer_id, connection=sc)
    except ValueError:
        demos = tuple(payment_envs.DEMOS)
        if not demos:
            raise
        # A simulated payment settles on no ledger: there is no rate to convert
        # through, and no on-chain value for a floor to protect.
        return [
            SettlementPlan(
                contract_hash=demos[0], ledger_tag=None,
                amount=amount, peer_amount=amount, is_demo=True,
            )
        ], []

    floors_by_contract = payment_envs.settlement_floors()
    plans: List[SettlementPlan] = []
    refusals: List[str] = []

    for system in systems:
        where = f"{system.ledger_tag}/{system.contract_hash[:12]}"
        system_amount = int(amount)
        try:
            if floor:
                system_amount = max(system_amount, full_deposit_mu(system))
            read_floors = floors_by_contract.get(system.contract_hash)
            minimum_output_mu = read_floors()[1] if read_floors else 0
        except Exception as e:
            # A moving fee that cannot be read right now, or a contract that has gone
            # away since the systems were matched. The others are still worth trying.
            refusals.append(f"{where}: could not read what it can settle ({e})")
            continue

        if system_amount < minimum_output_mu:
            if not floor:
                refusals.append(
                    f"{where}: {format_mu(system_amount)} is below the smallest output "
                    f"it can create ({format_mu(minimum_output_mu)}), so it cannot be "
                    "settled at all"
                )
                continue
            _l.LOGGER(
                f"Raising the automatic deposit for {peer_id} on {where} from "
                f"{format_mu(system_amount)} to {format_mu(minimum_output_mu)}: below "
                "that the ledger refuses to create the output at all."
            )
            system_amount = minimum_output_mu

        peer_amount = convert_mu(
            system_amount,
            from_mu_per_unit=system.local_mu_per_unit,
            to_mu_per_unit=system.peer_mu_per_unit,
        )
        if peer_amount <= 0:
            refusals.append(
                f"{where}: {format_mu(system_amount)} is worth less than a single one "
                "of the peer's MU"
            )
            continue

        plans.append(SettlementPlan(
            contract_hash=system.contract_hash,
            ledger_tag=system.ledger_tag,
            amount=system_amount,
            peer_amount=peer_amount,
        ))

    return plans, refusals


def deposit_refusal_reason(peer_id: str, amount: int) -> Optional[str]:
    """Why ``amount`` of our MU cannot be deposited on ``peer_id``, or None if it can.

    The same check `increase_deposit_on_peer` makes, offered up front so a command can
    print the reason to the operator instead of a bare failure. Runs the real thing
    rather than re-deriving the floors, so the message an operator reads cannot drift
    from the rule that actually stops the payment.

    "Can" means *any* shared payment system can carry it: a figure below Bitcoin's dust
    limit is not refused when the same peer also accepts ERG. When none can, every
    reason is reported rather than the first, because an operator choosing a new amount
    needs to know which floor to clear.

    Reads only local rows -- no wallet, no chain, no deposit token.
    """
    try:
        plans, refusals = __settlement_plans(peer_id=peer_id, amount=int(amount), floor=False)
    except ValueError as exc:
        return str(exc)
    if plans:
        return None
    reasons = "; ".join(refusals) or f"no payment system can settle {format_mu(int(amount))}"
    # Said once, about the whole answer rather than about each reason: what an operator
    # needs to know is that the refusal happened before anything touched a chain.
    return f"{reasons}; nothing was broadcast"


def increase_deposit_on_peer(peer_id: str, amount: int, on_transaction_url=None,
                             floor: bool = False) -> bool:
    """Deposit ``amount`` MU with ``peer_id``.

    ``amount`` is in *our* MU. What the peer is told is not: see `__deposit_amounts`.

    ``floor`` raises the amount to a full deposit when it is smaller, and belongs to the
    *automatic* refill only (``maintain.peer_deposits``), where nobody named a figure and
    a transfer worth less than its own fee is simply waste. An operator who typed an
    amount otherwise gets that amount, and `parse_to_mu` refuses to round their figure
    two calls earlier.

    ``floor`` carries the same meaning down into `__deposit_amounts`, which checks the
    ledger's *minimum output* -- the point below which no transaction can be built at
    all. An automatic refill is raised to it; a figure an operator typed is refused,
    before a deposit token is issued or the wallet is touched, rather than broadcast
    and rejected on-chain.

    Both floors are derived from what the ledger can settle, not configured; see
    src/payment_system/deposits.py.
    """
    try:
        plans, refusals = __settlement_plans(
            peer_id=peer_id, amount=int(amount), floor=floor
        )
    except ValueError as exc:
        _l.LOGGER(f"Cannot deposit on peer {peer_id}: {exc}.")
        return False
    if not plans:
        for refusal in refusals:
            _l.LOGGER(f"Cannot deposit on peer {peer_id}: {refusal}.")
        return False
    for refusal in refusals:
        # Not fatal: another system can carry it. Still worth reading -- it is the
        # difference between "this peer is expensive to pay" and "this peer is unpayable".
        _l.LOGGER(f"Skipping a payment system for peer {peer_id}: {refusal}.")

    _l.LOGGER(
        "Increase deposit on peer {} by {} (credited there as {} of its own MU), "
        "through {}".format(
            peer_id,
            format_mu(plans[0].amount),
            plans[0].peer_amount,
            " or ".join(
                f"{plan.ledger_tag or 'simulated'}/{plan.contract_hash[:12]}"
                for plan in plans
            ),
        )
    )
    try:
        settled = __peer_payment_process(
            peer_id=peer_id,
            plans=plans,
            on_transaction_url=on_transaction_url,
        )
        if settled:
            # The peer's MU, because that is the scale `balance_on_other_peer` reads
            # back from its Metrics and the scale `delegate_execution` compares a cost
            # against -- and *this* contract's conversion of it, because a figure from
            # another candidate would credit the peer an amount its own validator never
            # saw.
            if sc.add_balance_to_peer(peer_id=peer_id, balance_mu=settled.peer_amount):
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


def validate_payment_process(amount: int, ledger: celaut_pb2.Contract.Ledger, contract: bytes,
                             script: bytes, token: str, asset: str = "") -> bool:
    """Prove and credit one incoming payment.

    ``token`` is the *deposit* token -- the local UUID the payer wrote into the box'
    R4, which is what links the payment back to the client who made it. ``asset`` is
    the thing being paid *in*: the ledger's native unit ("ERG") or a token id. Two
    different things have historically been called "token" around this code; these two
    names are the distinction, and only the second one is money.
    """
    if not sc.deposit_token_exists(token_id=token, status='pending'):
        raise Exception(f"Deposit token {token} doesn't exists.")
    # Resolved once, up front, because the payment record needs it whichever way the
    # validation goes -- a deposit we refused is exactly the one a client will ask about.
    try:
        client_id: Optional[str] = sc.client_id_from_deposit_token(token_id=token)
    except Exception:
        client_id = None

    try:
        _r = bool(client_id) and __check_payment_process(
            amount=amount, ledger=ledger, token=token,
            contract=contract, script=script
        ) and _manager_module().increase_local_balance_for_client(client_id=client_id, amount_mu=amount)  # TODO allow for containers too.
    except: _r = False
    sc.update_deposit_token(token_id=token, status="payed" if _r else "rejected")
    contract_hash = sha3_256(contract).hexdigest() if contract else None
    if _r:
        # A share of what just arrived is owed as a donation, in the asset it arrived
        # in. Accrued here rather than paid here: see `donations.accrual`.
        _donation_accrual().accrue(
            amount_mu=amount,
            ledger=_ledger_tag(ledger),
            contract_hash=contract_hash,
            asset=asset,
        )
    # No tx id: an incoming payment is proved by an unspent box carrying the deposit
    # token in R4, and the transaction that created that box is not part of the proof.
    # The token is the link back to whoever paid, and it is on the row.
    sc.record_payment(
        direction='in',
        status='accepted' if _r else 'rejected',
        amount_mu=amount,
        client_id=client_id,
        deposit_token=token,
        ledger=_ledger_tag(ledger),
        contract_hash=contract_hash,
    )
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


def _pause_and_drain_deposits(timeout: int = DEPOSIT_DRAIN_TIMEOUT) -> bool:
    """Stop issuing deposit tokens and wait for the in-flight ones to settle.

    ``ergo.manager`` sweeps the wallet by SPENDING its boxes, while
    ``payment_process_validator`` proves an incoming payment by finding an
    *unspent* box carrying the deposit token in R4. A sweep that consumes that box
    turns a client's honest payment into a rejected one, so no sweep may run while
    a deposit is still in flight. Generation is paused for the wait because
    otherwise a busy node never reaches zero pending.

    Tokens past ``DEPOSIT_TOKEN_TTL`` are written off first, since a client's
    ``Payable`` call is the only thing that ever moves one out of 'pending'; without
    that, a single deposit nobody paid would block every future sweep. The timeout
    still bounds this iteration, for a token too young to expire but already dead:
    returning False skips the sweep, rather than wedging this thread and -- with
    generation paused -- locking out every future deposit as well.

    The pause exists only because the validator needs the box unspent.
    Proving payment from the confirmed transaction instead would remove the need
    for it entirely.
    """
    global deposit_generation_locked
    deposit_generation_locked = True
    expired = sc.expire_pending_deposit_tokens(DEPOSIT_TOKEN_TTL)
    if expired:
        _l.LOGGER(f"Expired {expired} deposit token(s) left unpaid for over {DEPOSIT_TOKEN_TTL}s.")
    deadline = monotonic() + timeout
    while sc.get_deposit_tokens(status="pending"):
        if monotonic() >= deadline:
            return False
        sleep(1)
    return True


def __manage_interfaces():
    global deposit_generation_locked
    while True:
        sleep(PAYMENT_MANAGER_ITERATION_TIME)
        _l.LOGGER("Execute payment manager iteration.")

        try:
            if not _pause_and_drain_deposits():
                _l.LOGGER(
                    f"{len(sc.get_deposit_tokens(status='pending'))} deposit token(s) still "
                    "pending after the drain timeout; skipping this iteration so their boxes "
                    "stay unspent."
                )
                continue

            _l.LOGGER("No pending deposit token, now payment interfaces can be managed.")

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
        finally:
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
