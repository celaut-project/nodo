from time import sleep
import os
import time
import traceback

from bee_rpc import client as beerpc

from protos import celaut_pb2 as celaut, celaut_pb2_grpc, celaut_pb2
from protos.gateway_bee import StartService_input_indices, StartService_input_message_mode
from src.manager.ddns import ddns_tick
from src.manager.energy import energy_tick
from src.manager.ergo import check_ergo_node_availability
from src.manager.manager import ALLOW_DEBT, accept_peer_refresh, descends_from_dev_client, ensure_dev_client_pools, stop_instance, spend_mu
from src.manager.metrics import balance_on_other_peer, instance_balance_on_peer
from src.payment_system.donations.indexer import tick as donations_tick
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.deposits import full_deposit_mu, refill_threshold_mu
from src.payment_system.mu_conversion import matching_payment_system
from src.payment_system.mu_conversion import peer_mu_in_local
from src.reputation_system.reasons import Reason
from src.utils import activity_window, demand_history
from src.utils import logger as log
from src.identity.grpc_transport import peer_channel
from src.utils.utils import peers_id_iterator
from src.utils.cost_functions.execution_cost import system_scarcity
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.monetary import format_mu
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils.java_dependency import JavaDependencyMissing, log_java_dependency_warning
from src.virtualizers.microvm.shares import resolved_disk_bytes
from src.virtualizers.interface import (
    janitor_cleanup_orphans as vm_janitor_cleanup_orphans,
    maintain as vm_maintain,
)
from src.core_services.low_demand import scheduler_tick

env_manager = ConfigManager()

SHORT_INTERVAL_COUNT = env_manager.get("SHORT_INTERVAL_COUNT")
SUBMIT_REPUTATION_AT_INIT = env_manager.get("SUBMIT_REPUTATION_AT_INIT")
MIN_SLOTS_OPEN_PER_PEER = int(env_manager.get("MIN_SLOTS_OPEN_PER_PEER"))
MANAGER_ITERATION_TIME = int(env_manager.get("MANAGER_ITERATION_TIME"))
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
CONFIGURED_HASH_ID = get_configured_hash_id(env_manager)

DEBUG_MODE = lambda: env_manager.get("DEBUG_MODE")

sc = SQLConnection()


def _row_arch(sys_req) -> str:
    """The ``arch`` column of a ``get_sys_req`` row, or None.

    Indexed defensively rather than as ``sys_req['arch']``: the row is a
    ``sqlite3.Row``, but every test that fakes one hands this loop a plain dict, and
    a KeyError here would stop the loop that charges *every* instance -- so a missing
    column costs the per-arch price of one instance, never the whole tick. Absent, it
    is charged the node's scalar memory price.
    """
    try:
        return sys_req["arch"]
    except (KeyError, IndexError, TypeError):
        return None

# It doesn't make sense to store this on disk (DB), as each of the elements in the set requires a search in the pairs to obtain a complete service. Therefore, the bottleneck is in the number of operations rather than the cost of the object in memory. Thus, what would make sense, as a control against attacks, is a maximum number of elements in the list, so that if it 'fills up,' no more elements can enter, and they are not searched until requested again at some other time when there is space.

# The mechanism uses two in-memory sets to manage service retrieval requests. The primary set, wanted_services, holds new service IDs to be fetched immediately, while the secondary set, wanted_services_retry, collects IDs that failed retrieval attempts so they can be retried later. This dual-set approach ensures that new requests are processed promptly while providing a controlled way to handle and periodically retry failed requests.

wanted_services = set()
wanted_services_retry = set()


def _payment_process_module():
    from src.payment_system import payment_process
    return payment_process


def _reputation_interface():
    from src.reputation_system import interface
    return interface

def add_wanted(service_id: str):
    if service_id not in wanted_services and service_id not in wanted_services_retry:
        log.LOGGER(f"Store the service hash on the wanted services set {service_id}")
        wanted_services.add(service_id)

def check_wanted_service(wanted: str):
    log.LOGGER(f"Check wanted service {wanted}")
    # Each execution of the function attempts to retrieve one of the services from the set. If the timeout is high or a large number of pairs are being processed, multiple calls might overlap if the function's execution time exceeds MANAGER_ITERATION_TIME; this is not an issue.
    
    _hash = celaut_pb2.Metadata.HashTag.Hash(
            type=CONFIGURED_HASH_ID,
            value=bytes.fromhex(wanted)
        )
    for peer in peers_id_iterator():
        """  TODO if get_service cost amount > 0

        if balance_on_other_peer(
                peer_id=peer,
        ) <= cost and not increase_deposit_on_peer(
            peer_id=peer,
            amount=cost
        ):
            raise Exception(
                'Get service error increasing deposit on ' + peer + 'when it didn\'t have enough '
                                                                        'balance.')
        """
        log.LOGGER(f"Taking the service {wanted} using peer {peer}")
        try:
            for b in beerpc.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        peer_channel(peer)
                    ).GetService,  # TODO An timeout should be implemented when requesting a service.
                    indices_serializer=celaut_pb2.Metadata.HashTag.Hash,
                    input=_hash,
                    indices_parser=StartService_input_indices,  #  Not used all the indices, but still are the same.
                    partitions_message_mode_parser=StartService_input_message_mode
            ):
                if  type(b) == beerpc.Dir:
                    log.LOGGER(f"    type of dir {b.type}")
                    
                if type(b) == celaut_pb2.Metadata:
                    log.LOGGER("Store the metadata.")
                    with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                        f.write(b.SerializeToString())
                elif type(b) == beerpc.Dir and b.type == celaut_pb2.Service:
                    log.LOGGER(f"Store the service {b.dir}")
                    os.system(f"mv {b.dir} {REGISTRY}{wanted}")
                    
            log.LOGGER(f"Wanted service {wanted} stored successfully.")
            return
        
        except Exception as e:
            log.LOGGER(f"Exception on peer {peer} getting the service {wanted}. {str(e)}.")
            continue
    log.LOGGER(f"Any peer was able to get the service {wanted}. (maybe there are not peers available)")
    wanted_services_retry.add(wanted)
            


# Instance outcomes already scored, as (vmachine_id, reason). Both of them are meant to
# end with the instance pruned, but pruning can fail -- a virtualizer that will not let
# go leaves the row in place, and the next sweep would score the same loss again, every
# ten seconds, for as long as it keeps failing. An instance can only be lost once.
_instances_penalised = set()


def _penalise_instance_once(vmachine_id: str, amount: int, reason: str):
    if (vmachine_id, reason) in _instances_penalised:
        return
    _instances_penalised.add((vmachine_id, reason))
    _reputation_interface().update_vmachine_reputation(
        vmachine_id=vmachine_id, amount=amount, reason=reason
    )


def maintain_vmachines(debug_mode: bool=False):
    def remove_and_penalize_vmachine(vmachine_id: str):
        _penalise_instance_once(vmachine_id, -100, Reason.INSTANCE_LOST)
        log.LOGGER(f"Prunning instance {vmachine_id} from the registry because the virtual machine does not exist.")
        try:
            stop_instance(token=vmachine_id)
        except Exception as e:
            log.LOGGER(f"Error prunning container {vmachine_id}: {e}")
    
    # One reading of system load for the whole sweep: every instance in this tick is
    # priced against the same machine state, and psutil is read once instead of per VM.
    scarcity = system_scarcity(force_refresh=True)

    live_instances = sc.get_all_internal_containers_ids()
    # An id is never reused, so once an instance is gone its entry can go too. Keeps the
    # set the size of what is running rather than of everything that ever ran.
    _instances_penalised.difference_update(
        {key for key in _instances_penalised if key[0] not in set(live_instances)}
    )

    # Summed as the sweep goes, so the hourly history costs one addition rather than a
    # second pass over the instances.
    charged_this_tick = 0

    for vmachine_id in live_instances:

        # Skip development vmachines from the ggconf command
        if "rundev" in vmachine_id:
            if debug_mode: log.LOGGER(f"Skipping development vmachine {vmachine_id}.")
            continue

        if debug_mode: log.LOGGER(f"Checking vmachine: {vmachine_id}")
        vm_maintain(vmachine_id=vmachine_id, debug_mode=debug_mode, remove_and_penalize=remove_and_penalize_vmachine)
        
        try:
            sys_req = sc.get_sys_req(id=vmachine_id)
        except Exception as e:
            # The vmachine may have been removed between get_all_internal_containers_ids() and get_sys_req()
            if debug_mode: log.LOGGER(f"Vmachine {vmachine_id} no longer exists in database: {e}")
            continue
            
        # Charge for the interval that just elapsed, so the price of an hour is the
        # same however often this node's manager ticks.
        #
        # Every resource the row records, including the CFS pair: passing only memory
        # and disk left `pricing.CPU_MU_PER_VCPU_HOUR` unbilled on the one path that
        # actually charges anybody, whatever it was set to.
        #
        # `arch` is not a resource -- it is what selects the memory price when the
        # operator prices memory per architecture. It comes off the row, written at
        # launch, so the tick never has to read a service off disk to price the
        # instance running it. A row from before the column existed reports None and
        # is charged the node's scalar memory price, exactly as it was.
        # A shared filesystem is part of the instance that exports it, and unlike
        # that instance's image -- a fixed-size file -- it grows after the launch
        # resolved how much disk the instance holds. So an exporter's disk is
        # re-derived here and written back to its row, which is the one figure
        # everything else reads: what this tick charges, what the host's disk
        # ceiling adds up, and what the next launch is admitted against. An
        # instance that exports nothing is not measured and its row is not
        # touched.
        resolved_disk = resolved_disk_bytes(vmachine_id)
        if resolved_disk is not None and resolved_disk != int(sys_req['disk_space'] or 0):
            if debug_mode:
                log.LOGGER(
                    f"{vmachine_id} disk with its shares: {resolved_disk} B "
                    f"(row had {sys_req['disk_space'] or 0} B)"
                )
            sc.update_sys_req(id=vmachine_id, mem_limit=None, disk_space=resolved_disk)
            sys_req = dict(sys_req)
            sys_req['disk_space'] = resolved_disk

        charge_mu = compute_maintenance_cost(
            system_resources=celaut.Sysresources(
                mem_limit=sys_req['mem_limit'] or 0,
                disk_space=sys_req['disk_space'] or 0,
                cpu_period=sys_req['cpu_period'] or 0,
                cpu_quota=sys_req['cpu_quota'] or 0,
            ),
            seconds=MANAGER_ITERATION_TIME,
            scarcity=scarcity,
            arch=_row_arch(sys_req),
        )
        if debug_mode:
            log.LOGGER(f"Charging {vmachine_id}: {format_mu(charge_mu)} for {MANAGER_ITERATION_TIME}s")

        if not spend_mu(id=vmachine_id, amount_mu=charge_mu, debug_mode=debug_mode):
            try:
                _penalise_instance_once(
                    vmachine_id, -10, Reason.INSTANCE_OUT_OF_BALANCE
                )
                log.LOGGER(f"Pruning container {vmachine_id} due to insufficient balance.")
                stop_instance(token=vmachine_id)
            except Exception as e:
                log.LOGGER(f'Error purging {vmachine_id}: {str(e)}')
                raise Exception(f'Error purging {vmachine_id}: {str(e)}')
        else:
            # No reputation for a charge that simply worked. This used to add +10 here,
            # written when `update_vmachine_reputation` did nothing at all -- and now
            # that it scores the service, a tick is the wrong thing to score by: at ten
            # seconds a service running a month would earn +2.6M, drowning every
            # penalty it ever took. A successful interval is the absence of a problem,
            # not a judgement about the service; the judgements are the two penalties
            # above (a machine lost, an instance that could not pay).
            #
            # The charge just succeeded, so this interval is a real cost the operator
            # paid: sample it for the burn-rate figure. Sourced from charge_mu (not a
            # balance diff) so a top-up between ticks never reads as negative spend. The
            # dev-vmachine skip above means no sample is recorded when nothing is charged.
            sc.record_instance_consumption(id=vmachine_id, charge_mu=charge_mu, seconds=MANAGER_ITERATION_TIME)
            charged_this_tick += int(charge_mu or 0)
            if debug_mode: log.LOGGER(f"Charged {vmachine_id} for the interval it just held.")

    # What this hour looked like (issue #337). Both figures were computed above and
    # were being dropped: the operator choosing the hours this node works in has
    # nothing else to read that choice against.
    demand_history.record_tick(
        instances_held=len(live_instances), mu_charged=charged_this_tick
    )

    # Reclaim what is running or on disk with no row behind it. Asked of the
    # virtualizer interface, not of a backend: reaching into `ch.maintain` for this
    # is what had the janitor judge QEMU guests with CH's liveness test (#295), and
    # `interface` is the only place that knows which backends this node has.
    vm_janitor_cleanup_orphans(debug_mode=debug_mode)


def maintain_delegated_instances(debug_mode: bool = False):
    """Charge a delegated instance for what the peer actually metered.

    A delegated child is funded exactly like a local one -- its father is charged the
    whole deposit at StartService -- and that deposit sits on the delegation row, in
    this node's MU. Nothing else charges it: the sweep above prices `local_instances`,
    and a delegated instance has no row there. Left unbilled, the deposit would still
    be whole when the instance stopped and the father would be handed back runtime he
    really used.

    What it costs is *measured*, not predicted: the peer's own figure for the child is
    read again and the difference against the last reading is what the client pays,
    converted at today's rate. A quote frozen at delegation would drift three ways at
    once -- the peer repricing its resources, the two nodes' MU rates moving against
    each other, and a hotplug making the child bigger than the shape it was quoted at
    -- and every one of those drifts is invisible from here. The subtraction happens
    on the peer's scale, before any conversion, so a rate that moved between two
    readings cannot read as consumption.

    A tick that cannot reach the peer charges nothing and loses nothing: the mark is
    only advanced by a reading that succeeded, so the next one that does charges
    everything metered since. A peer that answers but no longer knows the instance has
    stopped it on its own, and the row here follows it down.

    An instance that cannot pay is stopped, exactly as a local one is, and
    `stop_instance` hands its father whatever is left.
    """
    for row in sc.get_delegated_instances():
        peer_id, token, vmachine_id = row.get('peer_id'), row['token'], row.get('id')

        try:
            peer_balance_mu = instance_balance_on_peer(peer_id=peer_id, token=token)
        except Exception as e:
            if is_peer_available(peer_id=peer_id):
                # The peer is up and does not know this instance: it is gone there,
                # whatever ended it. Stopping it here settles the row the same way
                # any other stop does -- the peer answers a StopService for an
                # instance it does not have with a refund of zero, which is exactly
                # what it owes.
                log.LOGGER(
                    f"Peer {peer_id} no longer holds the delegated instance "
                    f"{vmachine_id} ({e}); stopping it here too."
                )
                try:
                    stop_instance(token=vmachine_id)
                except Exception as stop_error:
                    log.LOGGER(f"Error stopping the delegated instance {vmachine_id}: {stop_error}")
            elif debug_mode:
                log.LOGGER(
                    f"Peer {peer_id} is unreachable; not charging the delegated "
                    f"instance {vmachine_id} this tick."
                )
            continue

        spent_peer_mu = int(row.get('peer_balance_mu') or 0) - peer_balance_mu
        if spent_peer_mu <= 0:
            # Nothing metered, or the child was topped up between readings and holds
            # more than the mark. Either way there is nothing to charge; re-mark and
            # measure from here.
            sc.update_delegated_peer_balance(token=token, peer_balance_mu=peer_balance_mu)
            continue

        # Rounds up, and the client pays the remainder: this is a cost already
        # incurred with the peer, and the floor of it leaves this node paying the
        # difference on every tick for the life of the instance.
        charge_mu = peer_mu_in_local(peer_id, spent_peer_mu, round_up=True)
        if charge_mu is None:
            log.LOGGER(
                f"No common payment system with {peer_id} says what the "
                f"{spent_peer_mu} MU it metered for {vmachine_id} are worth here; "
                f"not charging this tick."
            )
            continue

        balance_mu = int(row.get('balance_mu') or 0)
        if balance_mu < charge_mu and not ALLOW_DEBT:
            log.LOGGER(
                f"Stopping delegated instance {vmachine_id} on peer {peer_id}: "
                f"{format_mu(balance_mu)} left will not cover the "
                f"{format_mu(charge_mu)} it just consumed there."
            )
            # Emptied before the stop, not left for `stop_instance` to refund: the
            # instance consumed more than it held, so every MU on the row is already
            # owed to the peer. Refunding it to the father would hand him back
            # runtime this node has paid for, and the shortfall the node absorbs is
            # then only the part the deposit could not reach.
            sc.update_delegated_balance(token=token, balance_mu=0)
            try:
                stop_instance(token=vmachine_id)
            except Exception as e:
                log.LOGGER(f"Error stopping the delegated instance {vmachine_id}: {e}")
            continue

        # Deposit and mark in one write, because they are one fact. Charging first and
        # marking second bills this same consumption again on the next tick if the
        # second never lands -- every `_execute` commits on its own, so two calls are
        # two transactions -- and marking first writes the consumption off instead.
        sc.update_delegated_deposit(
            token=token, balance_mu=balance_mu - charge_mu, peer_balance_mu=peer_balance_mu
        )
        if debug_mode:
            log.LOGGER(
                f"Charged delegated instance {vmachine_id} {format_mu(charge_mu)} for "
                f"what peer {peer_id} metered since the last tick."
            )


def enforce_activity_window(debug_mode: bool = False):
    """Reap what is still running once the activity window closes.

    Only under `activity_window.ON_CLOSE: stop`. The other setting, `refuse`, means the
    window governs admission alone: instances already running keep running and keep
    being charged, and the only thing that reaps them is an empty balance.

    Instances descended from a dev client are left alone, the same exemption
    `launch_service` applies at admission: the window is about renting this machine out
    after hours, not about killing the operator's own work at midnight.

    No reputation is scored either way. Being stopped by the clock is the operator's
    decision about their own machine, and says nothing at all about the service --
    unlike an instance the virtualizer lost, or one that could not pay.

    Runs after the charging sweep, so an instance is billed for the interval it really
    did hold before it is taken away. Its remaining balance is refunded by
    `stop_instance`, exactly as `nodo stop` would refund it.
    """
    if activity_window.is_open():
        return
    if not activity_window.stops_running_instances():
        if debug_mode:
            log.LOGGER("Outside the activity window; ON_CLOSE is not 'stop', so running "
                       "instances are left alone.")
        return

    for vmachine_id in sc.get_all_internal_containers_ids():
        if descends_from_dev_client(vmachine_id):
            if debug_mode:
                log.LOGGER(f"Instance {vmachine_id} descends from a dev client; the "
                           "activity window does not reap it.")
            continue
        log.LOGGER(
            f"Stopping instance {vmachine_id}: this node is outside its activity "
            f"window and activity_window.ON_CLOSE is 'stop'."
        )
        try:
            stop_instance(token=vmachine_id)
        except Exception as e:
            log.LOGGER(f"Error stopping {vmachine_id} at closing time: {e}")


def maintain_clients(debug_mode: bool=False):
    for client_id in SQLConnection().get_clients_id():
        if debug_mode: log.LOGGER(f"Maintain client {client_id}.")
        if SQLConnection().client_expired(client_id=client_id):
            log.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)


# Whether this process has already said that automatic refills are off.
_automatic_refill_announced = False


def _automatic_refill_enabled() -> bool:
    """May this tick fund a peer, and say so once when it may not.

    Said at normal level rather than only under DEBUG_MODE, and once rather than per
    peer per tick: an operator who forgot they set this would otherwise watch deposits
    run down with the node saying nothing, which reads as broken. Ten seconds between
    ticks is far too often to repeat it.

    The config is re-read every tick (it can be edited from the TUI while the node
    runs), so switching refills back on re-arms the announcement for the next time.
    """
    global _automatic_refill_announced
    enabled = bool(env_manager.get("deposits.AUTOMATIC_REFILL", True))
    if enabled:
        _automatic_refill_announced = False
        return True
    if not _automatic_refill_announced:
        _automatic_refill_announced = True
        log.LOGGER(
            "deposits.AUTOMATIC_REFILL is off: this node will not fund any peer on "
            "its own. Deposits run down until `nodo pay` or `nodo increase_peer_deposit` "
            "is run by hand."
        )
    return False


# Peers already penalised for being unreachable, so an outage costs one penalty and not
# one per tick. In memory on purpose: a restart re-arms it, which costs a single extra
# penalty for a peer that is still down, and keeps this out of the schema.
_peers_penalised_for_refresh = set()


def _penalise_unreachable_peer_once(peer_id: str):
    """Score a peer that cannot be refreshed, but only on the way down.

    This loop runs every MANAGER_ITERATION_TIME (10s by default) over every peer, so
    penalising on each pass meant a peer that was merely switched off lost 100 points
    every ten seconds -- some 864 000 a day. Reputation is meant to record that a peer
    failed us, not to count how often we noticed.
    """
    if peer_id in _peers_penalised_for_refresh:
        return
    _peers_penalised_for_refresh.add(peer_id)
    _reputation_interface().update_peer_reputation(
        peer_id=peer_id, amount=-100, reason=Reason.PEER_REFRESH_FAILED
    )


def _peer_is_reachable_again(peer_id: str):
    """Re-arm the penalty: the next outage is a new event, not the same one."""
    _peers_penalised_for_refresh.discard(peer_id)


def peer_deposits(debug_mode: bool = False):
    peer_ids = SQLConnection().get_peers_id()
    # Peers that no longer exist cannot come back, so drop them rather than let the set
    # grow with every peer this process ever failed to reach.
    _peers_penalised_for_refresh.intersection_update(peer_ids)

    for peer_id in peer_ids:
        if debug_mode: log.LOGGER(f"Starting check for peer {peer_id}.")

        # A peer that told us when its address expires is re-fetched once that moment
        # passes, without waiting for it to become unreachable first -- which is the
        # whole point of announcing the estimate (issue #236 point 9). Anticipating the
        # change costs one GetPeerInfo; missing it costs a failed delegation or payment.
        expiry = SQLConnection().get_peer_expiry_unix_timestamp(peer_id=peer_id)
        address_expired = bool(expiry) and expiry <= int(time.time())
        if address_expired and debug_mode:
            log.LOGGER(f"Peer {peer_id} announced its address expires at {expiry}; refreshing.")

        if address_expired or not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            if debug_mode: log.LOGGER(f"Peer {peer_id} needs a refresh. Attempting to fetch info.")

            try:
                peer = next(beerpc.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        peer_channel(peer_id=peer_id)
                    ).GetPeerInfo,
                    indices_parser=celaut_pb2.Peer,
                    partitions_message_mode_parser=True
                ), None)
                _peer_is_reachable_again(peer_id)
                if debug_mode: log.LOGGER(f"Successfully fetched info for peer {peer_id}.")
            except Exception as fetch_exception:
                _penalise_unreachable_peer_once(peer_id)
                continue

            if not peer:
                if debug_mode: log.LOGGER(f"No peer info found for {peer_id}. Skipping.")
                continue

            try:
                # Same signature check as an inbound IntroducePeer: whoever answers at
                # a stored address is not necessarily this peer (no TLS, and the
                # address may have been reassigned), and this refresh feeds the
                # payment contracts used further down this very loop.
                if not accept_peer_refresh(peer=peer, peer_id=peer_id):
                    if debug_mode: log.LOGGER(f"Refresh for peer {peer_id} was rejected.")
                    continue
                if debug_mode: log.LOGGER(f"Peer {peer_id} instance updated successfully.")
            except Exception as update_exception:
                log.LOGGER(f"[ERROR] Exception updating peer {peer_id}: {str(update_exception)}")
                continue
        else:
            _peer_is_reachable_again(peer_id)
            if debug_mode: log.LOGGER(f"Peer {peer_id} is available. Skipping info fetch.")

        # A deposit on a peer buys execution there and nothing else, so a node that
        # will not delegate must not keep topping one up: that is real ERG leaving the
        # wallet on-chain for a service it will never ask for. The refresh above still
        # runs -- knowing who is reachable stays useful (`nodo peers`, service
        # downloads) -- and `nodo pay` / `nodo increase_peer_deposit` still fund a peer
        # on demand, since an operator typing the command overrides the default policy.
        if not env_manager.get("network.DELEGATE_EXECUTION", True):
            if debug_mode:
                log.LOGGER(f"network.DELEGATE_EXECUTION is off; not funding peer {peer_id}.")
            continue

        # The switch for the node signing a payment on its own. Distinct from the one
        # above on purpose: an operator who wants to keep delegating but approve every
        # outgoing payment by hand had to turn delegation off to get it, which is not
        # the same thing at all. Gated here, before the balance query, so the tick makes
        # no request it cannot act on -- and `balance_on_other_peer` is not free: it is
        # an RPC that drops our client on the peer when it fails.
        if not _automatic_refill_enabled():
            if debug_mode:
                log.LOGGER(
                    f"deposits.AUTOMATIC_REFILL is off; leaving peer {peer_id} to be "
                    "funded by hand."
                )
            continue

        peer_balance = balance_on_other_peer(peer_id=peer_id)
        if debug_mode:
            log.LOGGER(f"Peer {peer_id} balance: {format_mu(peer_balance)}")

        # Sizing a deposit asks the payment contracts what they can settle, so it fails
        # when none is reachable -- inside the guarded block, because this loop is the
        # manager thread and nothing above it catches anything. Raising out of here would
        # stop every instance from being charged, which is how the `UnboundLocalError`
        # this function used to hold managed to take the whole node's billing down.
        try:
            # Sized for the system that will actually settle it -- the first one the
            # payer will try. One global figure would take the strictest floor across
            # every contract this node supports, so a node that also accepts Bitcoin
            # would demand a Bitcoin-sized deposit to top up an Ergo peer (#340 §5).
            payment_system = matching_payment_system(peer_id)
            refill_below = refill_threshold_mu(payment_system)
            if peer_balance >= refill_below:
                if debug_mode:
                    log.LOGGER(f"Peer {peer_id} has sufficient deposit: {format_mu(peer_balance)}.")
                continue

            log.LOGGER(f"[WARNING] The peer {peer_id} has not enough deposit.")
            # `floor=True` raises this to a full deposit if it is smaller, so log what
            # will actually be sent rather than the shortfall -- the two differ whenever
            # the peer still holds something.
            full_deposit = full_deposit_mu(payment_system)
            to_increase = max(full_deposit - peer_balance, full_deposit)
            if debug_mode:
                log.LOGGER(
                    f"Insufficient balance for {peer_id}:\n"
                    f"    - Current: {format_mu(peer_balance)}\n"
                    f"    - Refill below: {format_mu(refill_below)}\n"
                    f"    - Topping up by: {format_mu(to_increase)}"
                )

            increased = _payment_process_module().increase_deposit_on_peer(
                peer_id=peer_id, amount=to_increase, floor=True
            )
        except ValueError as e:
            # No payment system shared with this peer, or none that can size a deposit.
            # Skipped rather than raised: this loop is the manager thread and nothing
            # above it catches anything, so one unpayable peer must not stop every
            # instance from being charged.
            log.LOGGER(f"Cannot size a deposit for peer {peer_id}: {e}")
            continue
        except JavaDependencyMissing:
            log_java_dependency_warning(log.LOGGER, feature="Ergo payments or reputation")
            increased = False
        except Exception as e:
            # Whatever a contract's backend raises when it cannot be reached or will
            # not act. Sizing a deposit reaches the network now -- `refill_threshold_mu`
            # asks the settling contract for its floors, and Bitcoin's are a live fee
            # rate -- so this block fails for reasons that have nothing to do with this
            # peer: an unreachable bitcoind, an explorer that timed out, or a fee above
            # `MAX_FEE_RATE_SAT_VB`, which the backend reports by raising because
            # refusing to pay is the right answer.
            #
            # Caught by no contract's exception type on purpose: this loop must not
            # import a ledger to know what it throws, and the next ledger will throw
            # something else.
            #
            # Contained per peer and never re-raised. The loop above it is guarded
            # (`_manager_loop`), so an escaping exception no longer ends billing for the
            # life of the process -- but it would still cost every *other* peer this
            # pass, and put the whole tick on a backoff, for one unreachable node.
            log.LOGGER(f"Could not top up peer {peer_id}: {type(e).__name__}: {e}")
            continue

        if not increased:
            log.LOGGER(f"[ERROR] Manager error: the peer {peer_id} could not be increased.")
        elif debug_mode:
            log.LOGGER(f"Successfully increased deposit for {peer_id}.")


def check_dev_clients():
    ensure_dev_client_pools()


# How long to wait after a tick that raised, before trying the next one. The loop is
# guarded, so a failure that persists -- an unreachable database, a chain that answers
# nothing -- would otherwise be retried as fast as the machine can raise, filling the
# log and burning a core for no work done.
MANAGER_FAILURE_BACKOFF = 30


def manager_thread():

    log.LOGGER("Starting manager thread...")
    print("Starting manager thread...")
    
    # Functions to be executed at the beginning
    try:
        _payment_process_module().init_interfaces()
    except JavaDependencyMissing:
        log_java_dependency_warning(log.LOGGER, feature="Ergo payments or reputation")
    check_dev_clients()
    check_ergo_node_availability()
    # Publish the public IP right away rather than after a whole interval: a node
    # that just booted with a new address is exactly when the record is stalest.
    ddns_tick()
    if SUBMIT_REPUTATION_AT_INIT:
        try:
            _reputation_interface().submit_reputation(force_submit=True)
        except JavaDependencyMissing:
            log_java_dependency_warning(log.LOGGER, feature="Ergo payments or reputation")

    _manager_loop()


def _manager_loop() -> None:
    """Run the maintenance pass for ever, and survive a pass that raises.

    Every tick function this loop calls promises never to raise, and each of those
    promises is held up separately. That is a convention, not a structure, and the cost
    of one slip is out of all proportion to it: this runs on a daemon thread started in
    `serve.py` with nothing above it, so an escaping exception ends billing, the sweeps,
    the activity window and the donation indexer for the life of the process -- while
    the gRPC server keeps answering, so the node looks healthy from outside.

    A guard, not a supervisor. The thread is not restarted and no state is rebuilt: the
    next pass reads everything it needs from the database and the config anyway, so
    logging what happened and going round again is the whole of the recovery. What is
    deliberately not caught is `BaseException`: a `KeyboardInterrupt` is the node being
    stopped, and swallowing it would make this loop the reason it will not.
    """
    short_interval_count = 0
    while True:
        try:
            short_interval_count = _manager_pass(short_interval_count)
        except Exception:
            log.LOGGER(
                "[ERROR] The manager thread's maintenance pass raised. Billing, sweeps "
                f"and the periodic ticks resume in {MANAGER_FAILURE_BACKOFF}s; the "
                "function that raised is a bug, since every tick this loop calls is "
                f"meant to contain its own failures.\n{traceback.format_exc()}"
            )
            sleep(MANAGER_FAILURE_BACKOFF)


def _manager_pass(short_interval_count: int) -> int:
    """One maintenance pass, ending in the wait. Returns the next interval count."""
    if short_interval_count == int(SHORT_INTERVAL_COUNT):
        short_interval_count = 0
        
        # Functions to be executed every long interval
        check_ergo_node_availability()
        # submit_reputation()    TODO  https://github.com/celaut-project/nodo/issues/80
        check_dev_clients()
        if wanted_services_retry: 
            check_wanted_service(wanted_services_retry.pop())
    
    # Functions to be executed every short interval
    if wanted_services:
        check_wanted_service(wanted_services.pop())  # IMPORTANT! If you want to manually execute this function via a command, you must ensure thread safety.
    maintain_vmachines(debug_mode=DEBUG_MODE())
    maintain_delegated_instances(debug_mode=DEBUG_MODE())
    enforce_activity_window(debug_mode=DEBUG_MODE())
    maintain_clients(debug_mode=DEBUG_MODE())
    peer_deposits(debug_mode=DEBUG_MODE())

    # Opportunistic low-demand fallback scheduler (OFF unless low_demand.ENABLED).
    # Self-gates to low_demand.POLL_INTERVAL and never raises; see
    # src/core_services/low_demand.py and docs/design/low-demand-fallback.md.
    try:
        scheduler_tick()
    except Exception:
        pass

    # Node energy sample (issue #258). Self-gates to energy.SAMPLE_INTERVAL_SECONDS
    # and never raises. Informational only — does not feed MU pricing or low_demand.
    energy_tick()

    # Publish this node's public IP to its DDNS provider (OFF unless
    # ddns.ENABLED). Self-gates to ddns.INTERVAL_SECONDS and never raises.
    ddns_tick()

    # Read what other nodes donated, off each chain, into SQLite (issue #282).
    # Self-gates to its own hourly interval and never raises. This is the only
    # place donations are read from a network: the balancer reads the rows.
    donations_tick()

    sleep(MANAGER_ITERATION_TIME)
    if DEBUG_MODE():
        log.LOGGER(f"Long interval count: {short_interval_count}/{SHORT_INTERVAL_COUNT}.")
    return short_interval_count + 1
