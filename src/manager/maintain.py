from fractions import Fraction
from time import sleep
import os
import shutil
import threading
import time
import traceback
from typing import Dict, Optional

import grpc

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database import ledger
from src.manager import balance_statement, orphan_starts, statement_check
from src.manager.energy import energy_tick
from src.manager.ergo import check_ergo_node_availability
from src.manager.manager import ALLOW_DEBT, top_up_unlimited_clients, handle_internal_failure, revive_instance, suspend_instance, accept_peer_refresh, delegation_lock, descends_from_dev_client, ensure_dev_client_pools, forget_delegation_locks, stop_instance, spend_mu, get_client_id_on_other_peer, fetch_peer_info
from src.manager.metrics import balance_on_other_peer, instance_balance_on_peer
from src.utils.bee_client import BeeClient, Dir
from src.payment_system.donations.indexer import tick as donations_tick
from src.reputation_system.onchain_indexer import tick as onchain_reputation_tick
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.deposits import full_deposit_mu, refill_threshold_mu
from src.payment_system.mu_conversion import matching_payment_system
from src.reputation_system.reasons import Reason
from src.tunneling import delegated_endpoints
from src.utils import activity_window, dead_instances, demand_history
from src.utils import logger as log
from src.identity.grpc_transport import peer_channel
from src.utils.utils import from_amount, peers_id_iterator
from src.utils.cost_functions.execution_cost import RESOURCES, SCARCITY_SCALE, charge_at_launch_price, system_scarcity
from src.utils.cost_functions.resource_availability import _DEFAULT_CPU_PERIOD_US
from src.utils.cost_functions.general_cost_functions import RATE_STATEMENT_QUERY, RATE_STATEMENT_ROW, compute_maintenance_cost
from src.utils.monetary import format_mu, prices
from src.utils.hashing import get_configured_hash_id, get_configured_hash_spec, hash_stream
from src.utils.service_content import read_service_content
from src.utils.config import ConfigManager
from src.utils.java_dependency import JavaDependencyMissing, log_java_dependency_warning
from src.virtualizers.microvm.shares import resolved_disk_bytes
from src.virtualizers.interface import (
    janitor_cleanup_orphans as vm_janitor_cleanup_orphans,
    maintain as vm_maintain,
)
from src.core_services.low_demand import scheduler_tick
from src.manager.announcement_change import announcement_change_tick
from src.manager.gossip import gossip_pull_tick, gossip_push_tick

env_manager = ConfigManager()

SHORT_INTERVAL_COUNT = env_manager.get("SHORT_INTERVAL_COUNT")
SUBMIT_REPUTATION_AT_INIT = env_manager.get("SUBMIT_REPUTATION_AT_INIT")
MIN_SLOTS_OPEN_PER_PEER = int(env_manager.get("MIN_SLOTS_OPEN_PER_PEER"))
MANAGER_ITERATION_TIME = int(env_manager.get("MANAGER_ITERATION_TIME"))
# How long after a start that timed out here our account on the peer is read for it, when
# no read showed its movement yet (#578). The peer writes the movement when it takes the
# request, so one read after the timeout shows it.
UNSETTLED_START_READ_SECONDS = 86400
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
CONFIGURED_HASH_ID = get_configured_hash_id(env_manager)

DEBUG_MODE = lambda: env_manager.get("DEBUG_MODE")

sc = SQLConnection()


def _row_launch_bp(sys_req):
    """The price the instance was launched at, or None when the row does not carry it.

    None means no bound: the row is from before the columns existed, or a column is
    NULL for another reason, and the instance is charged at the current load. Indexed
    defensively for the same reason as `_row_arch`: a missing column costs one instance
    its bound, never the tick of every instance.
    """
    try:
        found = {
            "cpu": sys_req["launch_cpu_bp"],
            "mem": sys_req["launch_mem_bp"],
            "disk": sys_req["launch_disk_bp"],
        }
    except (KeyError, IndexError, TypeError):
        return None
    if any(value is None for value in found.values()):
        return None
    return {resource: int(value) for resource, value in found.items()}


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

# Where `nodo get <service_id>` (no `--now`) drops a marker file for this process to
# pick up. `wanted_services` above is in-memory and lives only inside whichever
# process runs this module's manager loop (`nodo serve` / the `nodo.service` daemon);
# a `nodo get` invocation is a separate, short-lived process and cannot reach it
# directly, so this directory is the one channel between the two. Drained every short
# interval by `drain_wanted_inbox`, straight into `add_wanted` -- the same set
# `abstract_input_service_iterable.py` already feeds when a delegated execution is
# missing a dependency -- so a queued `get` is retried exactly like any other want.
WANTED_INBOX_DIR = f"{env_manager.get('CACHE')}wanted/"


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


def drain_wanted_inbox():
    """Move whatever `nodo get` queued from disk into `wanted_services`.

    One file per queued id, named after it; the content is never read. Removed as
    soon as it is handed to `add_wanted`, so this directory is a mailbox, not a
    second, permanent record of what is wanted -- that stays the in-memory sets it
    always was.
    """
    try:
        queued = os.listdir(WANTED_INBOX_DIR)
    except FileNotFoundError:
        return

    for service_id in queued:
        add_wanted(service_id)
        try:
            os.remove(os.path.join(WANTED_INBOX_DIR, service_id))
        except OSError:
            pass

def check_wanted_service(wanted: str):
    log.LOGGER(f"Check wanted service {wanted}")
    # Each execution of the function attempts to retrieve one of the services from the set. If the timeout is high or a large number of pairs are being processed, multiple calls might overlap if the function's execution time exceeds MANAGER_ITERATION_TIME; this is not an issue.
    if not fetch_service_from_peers(wanted):
        wanted_services_retry.add(wanted)


# How long a peer's `GetService` may send nothing before it is given up on, in seconds
# (0: never). A silence, not a deadline: a large service takes long to arrive from a
# peer that is still sending, and must not read as one that stopped.
GET_SERVICE_IDLE_TIMEOUT = float(env_manager.get("GET_SERVICE_IDLE_TIMEOUT", 30))
# The ceiling on the whole transfer, in seconds (0: none), so a peer that keeps sending
# a byte now and then cannot hold the caller -- `nodo execute` among them -- forever.
GET_SERVICE_TIMEOUT = float(env_manager.get("GET_SERVICE_TIMEOUT", 1800))

# A peer that stalls costs what an unreachable one does (`PEER_REFRESH_FAILED`); one that
# sends other bytes under the id asked for costs ten times that: it is not down, it is
# wrong, and what it sent would have run had it not been hashed.
GET_SERVICE_TIMEOUT_PENALTY = -100
GET_SERVICE_WRONG_HASH_PENALTY = -1000

# (peer, service, reason) already scored. `wanted_services_retry` asks again every long
# interval for as long as no peer has the service, so without this one peer that stalls
# would lose points every few minutes, forever, for the same request.
_get_service_penalised = set()


def _penalise_get_service_once(peer_id: str, wanted: str, amount: int, reason: str):
    if (peer_id, wanted, reason) in _get_service_penalised:
        return
    _get_service_penalised.add((peer_id, wanted, reason))
    _reputation_interface().update_peer_reputation(peer_id=peer_id, amount=amount, reason=reason)


class _IdleWatchdog(grpc.StreamStreamClientInterceptor):
    """Cancels the call it intercepts once ``idle_s`` seconds pass with no response.

    Every response message counts, so a large service sent in many chunks keeps the
    call alive for as long as the chunks keep coming. ``fired`` tells a cancel by this
    watchdog apart from any other end of the call.
    """

    def __init__(self, idle_s: float):
        self.idle_s = idle_s
        self.fired = False
        self._last = time.monotonic()
        self._done = threading.Event()

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        call = continuation(client_call_details, request_iterator)
        self._last = time.monotonic()
        threading.Thread(target=self._watch, args=(call,), daemon=True).start()

        def responses():
            try:
                for response in call:
                    self._last = time.monotonic()
                    yield response
            finally:
                self._done.set()

        return responses()

    def _watch(self, call):
        while not self._done.wait(min(self.idle_s, 1.0)):
            if time.monotonic() - self._last >= self.idle_s:
                self.fired = True
                call.cancel()
                return


def _get_service_channel(peer: str):
    """The channel to ask ``peer`` for a service on, and its watchdog (None if off)."""
    channel = peer_channel(peer)
    if GET_SERVICE_IDLE_TIMEOUT <= 0:
        return channel, None
    watchdog = _IdleWatchdog(GET_SERVICE_IDLE_TIMEOUT)
    return grpc.intercept_channel(channel, watchdog), watchdog


def _is_timeout(e: Exception, watchdog) -> bool:
    if watchdog is not None and watchdog.fired:
        return True
    return isinstance(e, grpc.RpcError) and e.code() == grpc.StatusCode.DEADLINE_EXCEEDED


def _peer_sent_wanted(service_dir: str, wanted: str) -> bool:
    """Whether what a peer sent hashes to ``wanted``, the id the registry stores it under."""
    try:
        computed = hash_stream(read_service_content(service_dir), get_configured_hash_spec(env_manager)).hex()
    except Exception as e:
        log.LOGGER(f"Could not hash the service received for {wanted}: {e}")
        return False
    return computed == wanted


def fetch_service_from_peers(wanted: str) -> bool:
    """Ask each known peer's `GetService` for ``wanted``; True once one is in the registry.

    A peer is only trusted with the bytes, not with the id: what it sends is stored
    only if it hashes to ``wanted``, otherwise it is dropped, the peer penalised, and
    the next peer asked. A peer that goes ``GET_SERVICE_IDLE_TIMEOUT`` seconds without
    sending anything, or does not finish within ``GET_SERVICE_TIMEOUT``, is penalised
    too, and the next one asked.
    """
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
        watchdog = None
        try:
            client_id = get_client_id_on_other_peer(peer_id=peer)
            metadata = None
            service_dir = None
            channel, watchdog = _get_service_channel(peer)
            for b in BeeClient.get_service(
                    channel, _hash, client_id=client_id,
                    timeout=GET_SERVICE_TIMEOUT if GET_SERVICE_TIMEOUT > 0 else None,
            ):
                if  type(b) == Dir:
                    log.LOGGER(f"    type of dir {b.type}")

                if type(b) == celaut_pb2.Metadata:
                    metadata = b
                elif type(b) == Dir and b.type == celaut_pb2.Service:
                    service_dir = b.dir

            if not service_dir:
                log.LOGGER(f"Peer {peer} sent no service for {wanted}.")
                continue
            if not _peer_sent_wanted(service_dir, wanted):
                log.LOGGER(f"Peer {peer} sent a service that does not hash to {wanted}; dropped.")
                _penalise_get_service_once(
                    peer, wanted, GET_SERVICE_WRONG_HASH_PENALTY, Reason.GET_SERVICE_WRONG_HASH
                )
                if os.path.isdir(service_dir):
                    shutil.rmtree(service_dir, ignore_errors=True)
                elif os.path.exists(service_dir):
                    os.remove(service_dir)
                continue

            log.LOGGER(f"Store the service {service_dir}")
            shutil.move(service_dir, f"{REGISTRY}{wanted}")
            if metadata is not None:
                log.LOGGER("Store the metadata.")
                with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                    f.write(metadata.SerializeToString())

            log.LOGGER(f"Wanted service {wanted} stored successfully.")
            return True

        except Exception as e:
            if _is_timeout(e, watchdog):
                log.LOGGER(f"Peer {peer} timed out sending the service {wanted}.")
                _penalise_get_service_once(
                    peer, wanted, GET_SERVICE_TIMEOUT_PENALTY, Reason.GET_SERVICE_TIMED_OUT
                )
                continue
            log.LOGGER(f"Exception on peer {peer} getting the service {wanted}. {str(e)}.")
            continue
    log.LOGGER(f"Any peer was able to get the service {wanted}. (maybe there are not peers available)")
    return False
            


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


# How long an instance has to run after a revive before a new failure counts as a fresh
# one rather than another turn of a crash loop.
REVIVE_STABLE_AFTER_S = 120
# The longest wait between two attempts to start an instance that keeps failing. The
# wait grows with each failure and stops growing here; the attempts never stop, because
# an instance that still has MU is entitled to them and a loop that burns its own
# balance ends by itself when it runs out. The wait is only there so that a broken
# service does not saturate the host or fill the log.
REVIVE_BACKOFF_CAP_S = 60

# Per instance: consecutive failures, when it may be tried next, and when it last came
# back. In memory on purpose: a restart of the node is itself a reason to try again now.
_revive_failures: Dict[str, int] = {}
_revive_not_before: Dict[str, float] = {}
_revived_at: Dict[str, float] = {}


# At most this many instances are started again in one pass of the manager. A revive waits
# for the guest's boot, and after a host reboot every instance is waiting to be revived:
# started all at once in the one thread that also charges, sweeps and settles, they would
# stall everything else for as long as all of them take to boot. The rest wait one pass.
REVIVES_PER_PASS = 1


def _can_pay_a_tick(token: str, balance_mu: int, scarcity=None) -> bool:
    """Whether an instance holding ``balance_mu`` could pay the next maintenance tick.

    The tick suspends an instance whose balance is *below one tick's charge*, not only
    one at zero. Starting it again on any positive balance would have the next tick
    suspend it and the one after revive it: a boot and a kill every pass, for as long as
    the balance stays in between. So an instance comes back only when it can pay what
    the tick will ask -- the same figure, priced the same way. A node that allows debt
    never suspends for lack of MU, so there the balance is not in the way.
    """
    if ALLOW_DEBT:
        return True
    try:
        sys_req = sc.get_sys_req(id=token)
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
            launch_bp=_row_launch_bp(sys_req),
        )
    except Exception as e:
        log.LOGGER(f"Could not price a tick for {token}: {e}")
        return False
    return int(balance_mu) >= int(charge_mu)


def revive_backoff_seconds(failures: int) -> float:
    """The wait before the next attempt after ``failures`` in a row: none for the first
    (a crash is tried again at once), then 1 s, 2 s, 4 s ... up to the cap."""
    if failures <= 0:
        return 0.0
    return float(min(2 ** (failures - 1), REVIVE_BACKOFF_CAP_S))


def _note_instance_down(vmachine_id: str, now: Optional[float] = None) -> bool:
    """An instance's VM went down on its own: count it, and say when to try it again.

    One that had been running for a while is a fresh failure and starts from zero; one
    that went down soon after its last revive is a crash loop and waits longer each time.
    Returns True for a fresh failure, False for another turn of a loop.
    """
    now = time.monotonic() if now is None else now
    revived = _revived_at.get(vmachine_id)
    fresh = not (revived is not None and now - revived < REVIVE_STABLE_AFTER_S)
    if fresh:
        _revive_failures[vmachine_id] = 0
    else:
        _revive_failures[vmachine_id] = _revive_failures.get(vmachine_id, 0) + 1
    _revive_not_before[vmachine_id] = now + revive_backoff_seconds(_revive_failures[vmachine_id])
    return fresh


def revive_suspended_instances(now: Optional[float] = None) -> int:
    """Start again the suspended instances that have MU and are past their backoff.

    Two kinds wait here with the same disk: one that ran out of MU and was topped up,
    and one whose VM went down on its own. Both come back the same way, as the same
    instance, once they hold enough to pay a tick. One that fails to start is not given
    up: it stays suspended with its disk and waits longer before the next try. At most
    `REVIVES_PER_PASS` come back per pass. Returns how many came back.
    """
    now = time.monotonic() if now is None else now
    revived = 0
    try:
        suspended = sc.get_suspended_local_instances()
    except Exception as e:
        log.LOGGER(f"Error reading the suspended instances: {e}")
        return 0
    scarcity = None
    for row in suspended:
        if revived >= REVIVES_PER_PASS:
            break
        token = row["id"]
        try:
            balance = int(row.get("balance_mu") or 0)
        except (TypeError, ValueError):
            continue
        if balance <= 0:
            continue
        if now < _revive_not_before.get(token, 0.0):
            continue
        if scarcity is None:
            scarcity = system_scarcity()
        if not _can_pay_a_tick(token, balance, scarcity):
            continue
        try:
            if revive_instance(token):
                revived += 1
                _revived_at[token] = time.monotonic()
                log.LOGGER(f"Instance {token} is running again.")
        except Exception as e:
            _revive_failures[token] = _revive_failures.get(token, 0) + 1
            wait = revive_backoff_seconds(_revive_failures[token])
            _revive_not_before[token] = now + wait
            log.LOGGER(
                f"Could not revive {token} (failure {_revive_failures[token]}, "
                f"next try in {wait:.0f}s): {e}"
            )
    # An id is never reused, so an instance that no longer exists no longer needs its
    # entries. A running one keeps them: its next failure is counted against them.
    try:
        alive = set(sc.get_all_internal_containers_ids())
    except Exception:
        return revived
    for table in (_revive_failures, _revive_not_before, _revived_at):
        for token in [token for token in table if token not in alive]:
            table.pop(token, None)
    return revived


def maintain_vmachines(debug_mode: bool=False):
    # The instances this sweep found without their VM. Each is suspended or stopped by
    # `handle_internal_failure` before the charge below, and neither is charged a tick:
    # a suspended instance is charged nothing, and a dead one has settled already.
    lost_this_sweep = set()

    def remove_and_penalize_vmachine(vmachine_id: str, reason: str = dead_instances.PROCESS_LOST):
        lost_this_sweep.add(vmachine_id)
        # One penalty per failure, not per turn of a crash loop: a loss right after a
        # revive is the same failure again. One that comes after the instance ran for a
        # while is a new one, and scores again.
        if _note_instance_down(vmachine_id):
            _instances_penalised.discard((vmachine_id, Reason.INSTANCE_LOST))
        _penalise_instance_once(vmachine_id, -100, Reason.INSTANCE_LOST)
        # Revived if it can be (issue #594): it still has MU, so it stays afloat, and
        # what it wrote to its disk is not lost with the VM.
        log.LOGGER(f"Instance {vmachine_id} lost its virtual machine ({reason}).")
        try:
            handle_internal_failure(token=vmachine_id, reason=reason)
        except Exception as e:
            log.LOGGER(f"Error prunning container {vmachine_id}: {e}")
    
    # One reading of system load for the whole sweep: every instance in this tick is
    # priced against the same machine state, and psutil is read once instead of per VM.
    scarcity = system_scarcity(force_refresh=True)

    # Only the instances whose VM is meant to be running: a suspended one (issue #594)
    # has no process to check and no resources to charge, and finding it "lost" would
    # delete the very disk that suspending it kept.
    live_instances = sc.get_running_internal_containers_ids()
    # An id is never reused, so once an instance is gone its entry can go too. Keeps the
    # set the size of what is alive rather than of everything that ever ran. A suspended
    # instance is alive: it keeps its entry, so that the next turn of its crash loop
    # does not score the same failure again.
    if _instances_penalised:
        try:
            alive = set(sc.get_all_internal_containers_ids())
        except Exception:
            alive = None
        if alive is not None:
            _instances_penalised.difference_update(
                {key for key in _instances_penalised if key[0] not in alive}
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
        if vmachine_id in lost_this_sweep:
            continue

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
            # What this instance was launched at, so that a rise of the load since
            # then reaches it only as far as `pricing.MAX_PRICE_VOLATILITY` allows.
            # The clamp is in the cost function, the same one that the quote uses.
            launch_bp=_row_launch_bp(sys_req),
        )
        if debug_mode:
            log.LOGGER(f"Charging {vmachine_id}: {format_mu(charge_mu)} for {MANAGER_ITERATION_TIME}s")

        if not spend_mu(id=vmachine_id, amount_mu=charge_mu, debug_mode=debug_mode,
                        kind=ledger.MAINTENANCE,
                        # What the father checks this charge against (#570): the
                        # resources and the seconds it priced.
                        detail=statement_check.tick_detail(sys_req, MANAGER_ITERATION_TIME)):
            # No penalty for the service here. The father holds the balance of the
            # instance, and he can empty it on purpose with small top-ups (see
            # "Limit what an instance can spend" in docs/CONCEPTS.md). A death that the
            # father can cause is not evidence against the service. The cause stays on
            # record in `death_reason`. The delegated tick below has the same rule.
            try:
                if dead_instances.suspended_grace_seconds() > 0:
                    # Not a death: the father can still top it up. Its disk is kept
                    # until the grace period ends (`reap_expired_suspensions`).
                    log.LOGGER(f"Suspending container {vmachine_id} due to insufficient balance.")
                    suspend_instance(token=vmachine_id)
                else:
                    log.LOGGER(f"Pruning container {vmachine_id} due to insufficient balance.")
                    stop_instance(token=vmachine_id, reason=dead_instances.OUT_OF_BALANCE)
            except Exception as e:
                log.LOGGER(f'Error purging {vmachine_id}: {str(e)}')
                raise Exception(f'Error purging {vmachine_id}: {str(e)}')
        else:
            # No reputation for a charge that simply worked. This used to add +10 here,
            # written when `update_vmachine_reputation` did nothing at all -- and now
            # that it scores the service, a tick is the wrong thing to score by: at ten
            # seconds a service running a month would earn +2.6M, drowning every
            # penalty it ever took. A successful interval is the absence of a problem,
            # not a judgement about the service; the only judgement left here is the
            # penalty for a machine lost. An instance that could not pay is not one:
            # the father controls the balance.
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


def _peer_rates(peer_id: str, refresh: bool = False) -> dict:
    """The rates that ``peer_id`` announces. ``refresh`` asks the peer for its ``Peer``
    first: a period above the stored rates may be a new price that this node did not
    read yet, and that is not an overcharge."""
    if refresh:
        try:
            peer = fetch_peer_info(peer_channel(peer_id=peer_id), peer_id)
            if peer:
                accept_peer_refresh(peer=peer, peer_id=peer_id)
        except Exception as e:
            log.LOGGER(f"Could not refresh the rates of {peer_id}: {e}")
    return statement_check.stored_rates(peer_id)


def _our_mark(stored: dict) -> statement_check.Mark:
    """The mark that ``sc.get_peer_statement_mark`` returns, as a ``Mark``."""
    if not stored:
        return statement_check.NO_MARK
    return statement_check.Mark(stored["seq"], stored.get("hash", ""),
                                stored.get("balance_mu", 0), stored.get("ts"))


def _save_our_mark(peer_id: str, mark: statement_check.Mark,
                   check: Optional[statement_check.AccountCheck] = None,
                   queries: Optional[tuple] = None) -> None:
    sc.set_peer_statement_mark(
        peer_id, mark.seq, mark.hash, mark.balance, mark.ts,
        attempts=check.attempts if check else None,
        top_ups_taken=check.top_ups_taken if check else None,
        queries_verified=queries)


# The balance that paid for the last head of our account on each peer, when that head
# showed a balance that pays for no movement (``_check_our_account``).
_head_read_with: Dict[str, int] = {}


def _payable_balance(peer_id: str, metrics_balance: int) -> int:
    """What our account on ``peer_id`` pays reads with. That is the balance that
    ``Metrics`` gives, but never less than the signed floor: the balance that the peer
    signed, less what this node caused since then (``statement_check.signed_floor``).
    ``Metrics`` is free and not signed, and a peer can give a low one before each read."""
    return max(int(metrics_balance), statement_check.floor_on(peer_id, sc))


def _check_our_account(peer_id: str, debug_mode: bool = False) -> None:
    """Read the statement of our account on ``peer_id`` and check it (#570).

    Our account there pays the start of each child that this node delegates, and its
    top-ups; it gets our payments and the refunds. Nothing else may take MU from it, and
    the peer may only charge it for the statements that this node asked for.

    The read goes on from the mark, a page at a time, until it reaches the head of the
    account or ``ledger.MAX_STATEMENT_PAGES_PER_TICK`` pages. A page asks for no more
    movements than our balance there pays for, so a refusal for lack of balance is the
    peer contradicting the balance that it reports. The mark moves to the last movement
    that was checked, and what those movements took is accounted for with it, so a
    movement is judged once. A statement that does not continue the mark is scored, and
    the mark goes to the head: the movements up to there cannot be checked. A statement
    dated far from our clock is scored once a day, and the mark stays where it is.
    """
    client_id = sc.get_peer_client(peer_id=peer_id)
    if not client_id:
        return
    try:
        balance = _balance_held_on(peer_id)
    except Exception as e:
        if debug_mode:
            log.LOGGER(f"Cannot read our balance on {peer_id} to read our statement: {e}")
        return
    rates = statement_check.stored_rates(peer_id)
    mark = _our_mark(sc.get_peer_statement_mark(peer_id))
    balance = _payable_balance(peer_id, balance)

    for _ in range(ledger.max_statement_pages_per_tick()):
        rows_asked = statement_check.page_rows(balance, rates)
        if rows_asked <= 0:
            # The balance that the peer reports in ``Metrics`` is free and not signed: a
            # peer could report one that pays for nothing, just before each read. The
            # head of the account is signed, and costs a fixed part only.
            # A head that was read with this balance or a higher one showed that no
            # movement is paid for: it is read again only when the balance grows.
            payable = balance
            if payable > _head_read_with.get(peer_id, -1):
                signed = _signed_balance(peer_id, client_id, mark, balance, rates, debug_mode)
                if signed is not None:
                    balance = signed
                    rows_asked = statement_check.page_rows(balance, rates)
                    if rows_asked > 0:
                        _head_read_with.pop(peer_id, None)
                    else:
                        _head_read_with[peer_id] = payable
        else:
            _head_read_with.pop(peer_id, None)
        if rows_asked <= 0:
            log.LOGGER(f"Our balance on {peer_id}, {balance} MU, does not pay for a statement.")
            return
        from_seq = 1 if mark.seq is None else mark.seq + 1
        read_at = time.time()
        try:
            statement = statement_check.read(
                peer_id, client_id, from_seq=from_seq, rows=rows_asked)
        except Exception as e:
            # The peer answered ``GetMetrics`` in this tick, so a transport failure of the
            # statement alone can be its choice (``penalise_no_statement``).
            statement_check.penalise_no_statement(
                peer_id, client_id, e, seq=from_seq,
                pays=lambda: statement_check.still_pays(
                    peer_id, rows_asked, statement_check.floor_on(peer_id, sc)),
                answered=True, mark_ts=mark.ts)
            if debug_mode:
                log.LOGGER(f"No statement of our account on {peer_id}: {e}")
            return

        chain = statement_check.verify_chain(statement, mark, now=read_at, from_seq=from_seq)
        if chain.rows is None:
            statement_check.penalise_chain(peer_id, client_id, chain, from_seq)
            if not chain.clock:
                _save_our_mark(peer_id, statement_check.anchor_at_head(statement))
            return

        queries = [row for row in chain.rows if row.kind == ledger.STATEMENT_QUERY and row.amount < 0]
        queries_mu = -sum(row.amount for row in queries)
        statement_check.settle_failed_reads(
            peer_id, client_id, chain.rows, statement.head_seq, own=True, now=read_at,
            reaches_head=chain.reaches_head)
        check = statement_check.account_faults(
            chain.rows, sc.delegated_starts_on_peer(peer_id),
            [statement_check.StartAttempt(*a) for a in sc.start_attempts(peer_id)],
            [statement_check.TopUpReservation(*r) for r in sc.top_up_reservations(peer_id)],
            now=read_at, reaches_head=chain.reaches_head)
        faults = check.faults

        def counters():
            # The charges of this page are counted with the mark, when it is saved.
            out = dict(sc.get_statement_counters(peer_id))
            out["verified"] += len(queries)
            out["verified_mu"] += queries_mu
            return out

        charge_problems = statement_check.query_charge_problems(chain.rows, counters(), rates)
        if charge_problems:
            # The peer may have a price that this node did not read yet.
            rates = _peer_rates(peer_id, refresh=True)
            charge_problems = statement_check.query_charge_problems(chain.rows, counters(), rates)
        if charge_problems:
            faults.append((queries[-1].seq, "; ".join(charge_problems)))
        if faults:
            statement_check.penalise_once(
                peer_id, client_id, Reason.STATEMENT_OVERCHARGED,
                "; ".join(text for _, text in faults), min(seq for seq, _ in faults))

        mark = statement_check.mark_after(chain, statement, mark)
        _save_our_mark(peer_id, mark, check, (len(queries), queries_mu) if queries else None)
        if not chain.more:
            orphan_starts.stop_orphans(peer_id, client_id, statement_check.peer_call_timeout(), sc)
            return
        balance = _payable_balance(peer_id, from_amount(statement.balance))


def _signed_balance(peer_id: str, client_id: str, mark: statement_check.Mark, balance: int,
                    rates, debug_mode: bool = False) -> Optional[int]:
    """The balance of our account in the signed head of its statement, when the balance
    that ``Metrics`` gives does not pay for a read of movements. None when the head is
    not paid for either, or cannot be had.

    The head is checked against the mark like any statement is: a peer that shows another
    head than before is scored. It reads no movement, so the mark does not move.
    """
    head_price = int(rates.get(RATE_STATEMENT_QUERY, 0)) + int(rates.get(RATE_STATEMENT_ROW, 0))
    if balance < int(rates.get(RATE_STATEMENT_QUERY, 0)):
        return None
    read_at = time.time()
    try:
        statement = statement_check.read(peer_id, client_id, from_seq=0)
    except Exception as e:
        # The peer may refuse for lack of balance when the balance that it signed, less
        # what this node caused since, does not pay for the head either. Any other
        # refusal is the fault of ``penalise_no_statement``.
        statement_check.penalise_no_statement(
            peer_id, client_id, e, seq=0,
            pays=lambda: statement_check.floor_on(peer_id, sc) >= head_price,
            answered=True, mark_ts=mark.ts)
        if debug_mode:
            log.LOGGER(f"No head of our account on {peer_id}: {e}")
        return None
    chain = statement_check.verify_chain(statement, mark, now=read_at, from_seq=0)
    if chain.rows is None:
        statement_check.penalise_chain(peer_id, client_id, chain, 0)
        return None
    return from_amount(statement.balance)


def _check_maintenance_quote(row: dict, rows, since_ts: Optional[int]) -> None:
    """Score the peer of ``row`` when ``rows``, the movements of the child that were just
    checked after the mark dated ``since_ts``, charge more maintenance than the peer
    quoted for the child (#567, ``statement_check.maintenance_overcharge``).

    The quote is the one that the peer gave for this instance, kept on the row in its MU
    (``quote_*``), not its rates of today: a peer that raised its prices since is held to
    the price that it signed. It is for the most resources of the service, so a hotplug
    up to them is not an overcharge. A row with no quote is not checked.

    Only the peer is scored, with ``MAINTENANCE_OVERCHARGED``: not the service and not
    the client. A price above the quote shows at every period, so it is scored at most
    once in ``statement_check.FAULT_WINDOW_SECONDS`` for each child, and once for each
    movement. The charge of the child and the instance do not change: this node absorbs
    the excess. Never raises.
    """
    try:
        fault = statement_check.maintenance_overcharge(
            rows, statement_check.MaintenanceQuote.of_row(row), since_ts)
        if fault is None:
            return
        seq, problem = fault
        statement_check.penalise_once(
            row.get('peer_id'), row['token'], Reason.MAINTENANCE_OVERCHARGED, problem, seq,
            window=statement_check.FAULT_WINDOW_SECONDS, per_account=True)
    except Exception as e:
        log.LOGGER(f"Could not check the maintenance of {row.get('id')} against its quote: {e}")


def _balance_held_on(peer_id: str) -> int:
    """What we hold on ``peer_id`` now, in its MU: read for free from its ``Metrics``."""
    from src.manager.metrics import refresh_balance_on_peer
    return refresh_balance_on_peer(peer_id)


def _read_child(row: dict, debug_mode: bool = False, force_statement: bool = False):
    """What the child owes for its consumption on the peer, and the state to keep.

    Returns ``(charge_peer_mu, state)`` in the MU of the peer, and raises when nothing
    could be read. ``charge_peer_mu`` is signed: a reading that shows a past charge to
    have been too much gives MU back. ``state`` is what ``settle_delegated_tick`` writes, and
    ``rows``: the movements of the statement that were checked, which ``settle_child``
    reads the resources of the child from.

    What is owed is the sum of two things (the totals are in `delegated_instances`):

    * what the statements that were checked show the child spent (``verified``);
    * what its balance on the peer has fallen by since the last of them, once what its
      father moved by ``ModifyDeposit`` is taken out. That is read for free, from the
      ``Metrics`` of the peer, at every tick.

    ``owed = verified + (balance at the mark - balance now) + (sent - verified sent)``,
    and the charge is ``owed`` less what was charged already. The statement is read every
    ``ledger.STATEMENT_READ_EVERY_TICKS`` ticks, and it replaces the second part by the
    first for what it covers: the next charge corrects whatever the estimate got wrong.
    What was sent is read before the peer is asked, so a ``ModifyDeposit`` that lands
    in between can only make the charge smaller for a moment, never wrongly stop an
    instance for a charge that the father caused.

    A child with no mark (the statement of its start could not be used) is read from its
    first movement and checked from the genesis of its account, and what opened the
    account (``start``, ``build``) is not consumption: the build that the peer took may
    be more than the quote that the balance was measured from.

    When the statement does not continue the mark (a fault of the peer, or movements that
    the retention of the peer deleted) nothing can be checked up to its head: what the balance says is taken for what was spent, and the mark goes to the
    head. A fault is scored, and a gap that the retention explains is not. A charge that
    comes from a balance, and not from a statement that continues the mark, is never
    negative: only a checked statement gives MU back.

    A statement is asked for only when our balance on the peer pays for it: a refusal for
    lack of balance is then the peer contradicting the balance that it reports, and when
    it does not, the tick goes on from the balance and no one is scored.

    The movements of a statement that continues the mark are also checked against the
    maintenance quote of the peer (``_check_maintenance_quote``). That changes nothing
    in what is returned.
    """
    peer_id, token = row.get('peer_id'), row['token']
    # With no seq the mark is not a movement, and its balance is the last reading.
    mark = statement_check.Mark(
        row.get('peer_statement_seq'), row.get('peer_statement_hash') or "",
        int(row.get('peer_balance_mu') or 0), row.get('peer_statement_ts'))
    verified = int(row.get('verified_consumption_peer_mu') or 0)
    verified_sent = int(row.get('verified_father_net_peer_mu') or 0)
    sent = int(row.get('father_net_peer_mu') or 0)
    charged = int(row.get('charged_peer_mu') or 0)
    read_at = time.time()
    from_seq = 1 if mark.seq is None else mark.seq + 1

    statement, error, asked, checked = None, None, False, False
    rows = ()
    if force_statement or mark.seq is None or sc.child_statement_due(
            token, ledger.statement_read_every_ticks(MANAGER_ITERATION_TIME)):
        try:
            rows_asked = statement_check.page_rows(
                _payable_balance(peer_id, _balance_held_on(peer_id)),
                statement_check.stored_rates(peer_id))
            if rows_asked <= 0:
                raise RuntimeError(f"our balance on {peer_id} does not pay for a statement")
            asked = True
            statement = statement_check.read(
                peer_id, get_client_id_on_other_peer(peer_id=peer_id), token=token,
                from_seq=from_seq, rows=rows_asked,
            )
        except Exception as e:
            error = e

    if statement is not None:
        # No mark (the statement of the start could not be used) and the statement shows
        # the account from its first movement: it is checked from its genesis, like the
        # one that ``_check_start`` reads, and what opened the account is not consumption.
        start = mark
        if mark.seq is None and statement.movements and statement.movements[0].seq == 1:
            start = statement_check.Mark.new_account(token, 0)
        chain = statement_check.verify_chain(statement, start, now=read_at, from_seq=from_seq)
        if chain.clock:
            # Dated far from our clock: nothing in it can be used, and the clock that is
            # wrong can be ours. It is scored once a day, and the tick goes on from the
            # balance, as with no statement.
            statement_check.penalise_chain(peer_id, token, chain, from_seq)
            statement, error, asked = None, None, False
    if statement is not None:
        if chain.rows is not None and start.seq is not None and not chain.gap:
            verified += statement_check.consumption(chain.rows)
            verified_sent += statement_check.father_net(chain.rows)
            new = statement_check.mark_after(chain, statement, start)
            balance_now = from_amount(statement.balance)
            checked = True
            rows = tuple(chain.rows)
            statement_check.settle_failed_reads(peer_id, token, chain.rows, statement.head_seq)
            _check_maintenance_quote(row, chain.rows, start.ts)
        else:
            if chain.problem:
                statement_check.penalise_chain(peer_id, token, chain, from_seq)
            new = statement_check.anchor_at_head(statement)
            balance_now = new.balance
            # What cannot be checked is taken from the balance. Never below 0: a reading
            # that nothing backs does not give MU back.
            verified += max(0, (mark.balance - balance_now) + (sent - verified_sent))
            verified_sent = sent
    else:
        try:
            balance_now = instance_balance_on_peer(peer_id=peer_id, token=token)
        except Exception as metrics_error:
            # Neither answer: the peer is down, or it does not know the instance. The
            # caller tells the two apart.
            if statement_check.unreachable(metrics_error):
                raise metrics_error from None
            raise (error or metrics_error) from None
        # The peer knows the instance and does not give its statement. It just answered
        # ``GetMetrics``, so a transport failure of the statement alone can be its choice.
        if asked:
            statement_check.penalise_no_statement(
                peer_id, token, error, seq=from_seq, child=True,
                pays=lambda: statement_check.still_pays(
                    peer_id, rows_asked, statement_check.floor_on(peer_id, sc)),
                answered=True, mark_ts=mark.ts)
        new = mark
        if debug_mode:
            log.LOGGER(f"Charging {row.get('id')} from the balance on {peer_id}, not from its statement.")

    owed = verified + (new.balance - balance_now) + (sent - verified_sent)
    charge = owed - charged
    if not checked:
        # Only what a statement was checked against can give MU back. A balance that the
        # peer reports could be a lie that credits the child, and the next statement
        # corrects an estimate that was too low.
        charge = max(0, charge)
    return charge, {
        "peer_balance_mu": new.balance, "seq": new.seq, "hash": new.hash or None, "ts": new.ts,
        "verified_consumption": verified, "verified_father_net": verified_sent,
        "charged_delta": charge, "rows": rows,
    }


SHAPE_KEYS = ("mem_limit", "disk_space", "cpu_period", "cpu_quota")
# The multipliers of a launch while the node is free: the base price.
BASE_LAUNCH_BP = {resource: SCARCITY_SCALE for resource in RESOURCES}


def _sysresources(shape) -> celaut.Sysresources:
    return celaut.Sysresources(**{key: int(shape.get(key) or 0) for key in SHAPE_KEYS})


def _delegated_price(row: dict, shape, seconds: float) -> int:
    """Our price for ``shape`` held ``seconds``, at the multipliers of the launch of ``row``.

    A delegated instance is not free, and the scarcity of this node says nothing about
    it (``delegated_price``).
    """
    return charge_at_launch_price(system_resources=_sysresources(shape), seconds=seconds,
                                  launch_bp=_row_launch_bp(row) or BASE_LAUNCH_BP,
                                  arch=_row_arch(row))


def _shape(row: dict, prefix: str = "") -> Optional[dict]:
    """The shape in the columns of ``row`` that start with ``prefix``, or None if unset."""
    values = [row.get(f"{prefix}{key}") for key in SHAPE_KEYS]
    if any(value is None for value in values):
        return None
    return {key: int(value) for key, value in zip(SHAPE_KEYS, values)}


def _vcpus(shape) -> Fraction:
    # A period of 0 is the default period of the kernel, as ``requested_units`` reads it.
    return Fraction(int(shape["cpu_quota"]), int(shape["cpu_period"]) or _DEFAULT_CPU_PERIOD_US)


def _changed_by(start: dict, peer_start: dict, peer_now: dict) -> dict:
    """Our shape at the start, changed as much as the resources on the peer changed.

    The floors of the two nodes are not the same (the disk of an image that one of them
    has built, ``MIN_MEM_MIB``, the vCPU of a service that declares none), so the figures
    of the peer are not compared with ours: only their changes are taken.
    """
    shape = dict(start)
    for key in ("mem_limit", "disk_space"):
        shape[key] = max(0, start[key] + peer_now[key] - peer_start[key])
    vcpus = max(Fraction(0), _vcpus(start) + _vcpus(peer_now) - _vcpus(peer_start))
    shape["cpu_quota"] = int(vcpus * (start["cpu_period"] or _DEFAULT_CPU_PERIOD_US))
    return shape


def _capped(shape: dict, row: dict) -> dict:
    """``shape`` held down to the most that the child is charged for (``max_*`` on its row).

    The changes come from the peer: a peer that wrote more than the service can hold
    would raise what this node charges its own client.
    """
    capped = dict(shape)
    for key in ("mem_limit", "disk_space"):
        if row.get(f"max_{key}") is not None:
            capped[key] = min(capped[key], int(row[f"max_{key}"]))
    if row.get("max_cpu_quota") is not None and row.get("max_cpu_period") is not None:
        most = {"cpu_quota": int(row["max_cpu_quota"]), "cpu_period": int(row["max_cpu_period"])}
        if _vcpus(capped) > _vcpus(most):
            capped["cpu_period"], capped["cpu_quota"] = most["cpu_period"], most["cpu_quota"]
    return capped


def _period_of(movement) -> Optional[tuple]:
    """``(shape, seconds)`` of a ``maintenance`` movement of the peer, from its detail
    (``statement_check.tick_detail``), or None if it does not say them."""
    if movement.kind != ledger.MAINTENANCE or movement.detail.get("adjustment"):
        return None
    try:
        shape = {key: int(movement.detail[key]) for key in SHAPE_KEYS}
        return shape, int(movement.detail["seconds"])
    except (KeyError, TypeError, ValueError):
        return None


def measured_shape(row: dict, rows) -> tuple:
    """What the resources that the child held on the peer change in its charge (#577).

    The service changes its resources on the peer (``ModifyServiceSystemResources`` goes
    to the node that runs it), so this node only knows them from the statement of the
    child: each ``maintenance`` movement says the resources and the seconds that it
    priced. The first that a statement shows is the start on the peer (``peer_base_*``),
    and the shape of the child is our shape at the start (``base_*``) changed as those
    resources changed since then (``_changed_by``), held down to its most (``_capped``).
    A change of the resources before the first maintenance of the peer is not seen.

    The ticks between two statements charge the last shape that was known, the one on the
    row. For each period in ``rows`` the correction is our price for the shape of the
    child less our price for the shape that was charged: positive if it grew, negative if
    it shrank.

    Returns ``(correction_mu, last_shape, resource_changes, peer_base)``: the correction,
    the last shape that ``rows`` show (None if they show none), the number of
    ``resource_change`` movements that the peer did not give back (each is charged at our
    rate), and the start on the peer when ``rows`` are the first to show it (else None).
    """
    known = _shape(row) or {key: 0 for key in SHAPE_KEYS}
    start = _shape(row, "base_") or known
    peer_start = _shape(row, "peer_base_")
    new_peer_start = None
    correction, last = 0, None
    changes, given_back = set(), set()
    for movement in rows:
        if movement.kind == ledger.RESOURCE_CHANGE:
            changes.add(movement.seq)
            continue
        if movement.kind == ledger.REFUND:
            try:
                given_back.add(int(movement.detail.get("refunds_seq")))
            except (TypeError, ValueError):
                pass
            continue
        period = _period_of(movement)
        if period is None:
            continue
        peer_now, seconds = period
        if peer_start is None:
            peer_start = new_peer_start = peer_now
        shape = _capped(_changed_by(start, peer_start, peer_now), row)
        if shape != known:
            correction += _delegated_price(row, shape, seconds) - _delegated_price(row, known, seconds)
        last = shape
    return correction, last, len(changes - given_back), new_peer_start


def _unmoved_state(row: dict) -> dict:
    """The mark of ``row`` as it is: for a charge with no reading of the peer."""
    return {"peer_balance_mu": int(row.get('peer_balance_mu') or 0),
            "seq": row.get('peer_statement_seq'), "hash": row.get('peer_statement_hash'),
            "ts": row.get('peer_statement_ts')}


def settle_child(row: dict, charge_peer_mu: int, state: dict, *, stop_if_short: bool = True,
                 debug_mode: bool = False) -> bool:
    """Charge the child what this node advertises for the time since the last charge, and
    write what the peer charged this node.

    A delegated child is charged like a local one (#577): our cost function and our
    rates, for the seconds since ``charged_until`` and for the last shape that is known of
    it (the one on its row), at the multipliers of its launch (``_delegated_price``).
    When a statement was read, ``rows`` in ``state`` correct the periods that it covers by
    the shape that the child really held, and the changes of its resources are charged at
    our rate (``measured_shape``). The last shape goes on the row for the next ticks.

    What the peer charged this node (``charge_peer_mu``, in the MU of the peer) is kept
    in the totals of ``state``, and is no part of the charge: the difference is the
    margin of this node, and a peer that charges more is a loss that this node takes.
    With no reading of the peer, ``state`` is the mark as it is (``_unmoved_state``).

    Writes the rows, the totals and the mark in one transaction (``settle_delegated_tick``).
    With ``stop_if_short`` an instance whose deposit does not cover the charge is stopped,
    as a local one is. Returns True.
    """
    peer_id, token, vmachine_id = row.get('peer_id'), row['token'], row.get('id')
    state = dict(state)
    rows = state.pop("rows", ())
    now = time.time()
    since = row.get('charged_until')
    seconds = max(0.0, now - float(since)) if since else 0.0
    state["charged_until"] = now
    charge_mu = _delegated_price(row, _shape(row) or {}, seconds)
    detail = statement_check.tick_detail(row, seconds)

    movements = []
    correction, last, changes, peer_start = measured_shape(row, rows)
    if correction:
        movements.append(ledger.Movement(ledger.MAINTENANCE, -correction,
                                         detail={"adjustment": "1"}))
    for _ in range(changes):
        movements.append(ledger.Movement(ledger.RESOURCE_CHANGE, -int(prices().modify_resources_mu)))
    if last is not None:
        state["shape"] = last
    if peer_start is not None:
        state["peer_base"] = peer_start
    total_mu = charge_mu - sum(m.amount_mu for m in movements)

    balance_mu = int(row.get('balance_mu') or 0)
    if stop_if_short and balance_mu < total_mu and not ALLOW_DEBT:
        log.LOGGER(
            f"Stopping delegated instance {vmachine_id} on peer {peer_id}: "
            f"{format_mu(balance_mu)} left will not cover the "
            f"{format_mu(total_mu)} it just consumed."
        )
        # Emptied before the stop, not left for `stop_instance` to refund: the
        # instance consumed more than it held, so every MU on the row is already
        # owed. Refunding it to the father would hand him back runtime this node
        # has paid for, and the shortfall the node absorbs is then only the part the
        # deposit could not reach.
        sc.settle_delegated_tick(token=token, charge_mu=balance_mu, mark=state, detail=detail)
        # No reputation penalty, the same as in the local tick: the father holds the
        # balance, so this death is not a fault of the service.
        try:
            stop_instance(token=vmachine_id, reason=dead_instances.OUT_OF_BALANCE)
        except Exception as e:
            log.LOGGER(f"Error stopping the delegated instance {vmachine_id}: {e}")
        return True

    # Charge and mark in one transaction, because they are one fact. Charging first
    # and marking second bills this same time again on the next tick if the second
    # never lands, and marking first writes the time off instead.
    sc.settle_delegated_tick(token=token, charge_mu=charge_mu, mark=state,
                             detail=detail if charge_mu else None, movements=movements)
    if debug_mode:
        log.LOGGER(f"Charged delegated instance {vmachine_id} {format_mu(total_mu)} for {seconds:.0f}s.")
    return True


def close_delegated_charge(external_token: str) -> None:
    """Settle what a delegated child owes right before it is stopped.

    The account of the child on the peer ends with the stop, and its resources since the
    last reading would stay at the estimate. This reads its statement one last time and
    charges with it. A peer that cannot be read does not stop the charge of the time,
    which does not need it. Never raises, and never stops the instance for a charge: the
    stop is under way.
    """
    try:
        row = next((r for r in sc.get_delegated_instances() if r['token'] == external_token), None)
        if row is None:
            return
        try:
            charge_peer_mu, state = _read_child(row, force_statement=True)
        except Exception as e:
            log.LOGGER(f"Could not read the delegated instance before its stop; "
                       f"charging its time without it: {e}")
            charge_peer_mu, state = 0, _unmoved_state(row)
        settle_child(row, charge_peer_mu, state, stop_if_short=False)
    except Exception as e:
        log.LOGGER(f"Could not close the charge of the delegated instance before its stop: {e}")


def maintain_delegated_instances(debug_mode: bool = False):
    """Charge a delegated instance what this node advertises, and measure what the peer charges.

    A delegated child is funded like a local one -- its father is charged `initial_mu`
    at StartService, and the build of this node comes out of it (#563) -- and what the
    father can get back sits on the delegation row, in this node's MU. Nothing else
    charges it: the sweep above prices `local_instances`, and a delegated instance has no
    row there. Left unbilled, the deposit would still be whole when the instance stopped
    and the father would be handed back runtime he really used.

    The charge is this node's price (#577): the cost function of a local instance, the
    shape on the row, our rates and our scarcity, for the time since the last charge
    (``settle_child``). A peer that reprices does not reach the child. The service changes
    its resources on the peer, and the statement of the child shows them: each statement
    that is read corrects the shape that the ticks charged (``measured_shape``).

    What the peer charges this node is *measured* all the same: it is the cost of the
    delegation, and the margin is the difference. The balance of the child on the peer
    falls by what it consumed, and its statement (#570) says exactly what that was. The
    sum happens on the peer's scale, before any conversion, so a rate that moved between
    two readings cannot read as consumption.

    Every tick estimates from the balance, which the peer gives for free, and every
    ``ledger.STATEMENT_READ_EVERY_TICKS`` ticks the statement replaces the estimate with
    what the movements say. The chain of the child is checked, and its maintenance against
    the quote that the peer gave for it: a peer that charges more is scored, and this node
    absorbs the excess (#567, ``_check_maintenance_quote``). The next reading corrects the
    estimate, up or down (``_read_child``).

    Our own account on each peer is checked first: it pays the starts and the top-ups
    of these children, and nothing else may take MU from it.

    A tick that cannot reach the peer still charges the time, at the last shape that is
    known: our price does not need the peer, and an instance that cannot pay is stopped
    all the same. What the peer charged loses nothing: the totals are only advanced by a
    reading that succeeded, so the next one that does measures everything since. A peer
    that answers but no longer knows the instance has stopped it on its own, and the row
    here follows it down.

    An instance that cannot pay is stopped, exactly as a local one is, and
    `stop_instance` hands its father whatever is left.
    """
    delegated = sc.get_delegated_instances()
    every = ledger.statement_read_every_ticks(MANAGER_ITERATION_TIME)
    with_children = {row.get('peer_id') for row in delegated if row.get('peer_id')}
    # A start that timed out here is read for as a child is (#578): the peer may have
    # charged it, and its child is then stopped (``orphan_starts``).
    unsettled = set(sc.peers_with_unsettled_starts(time.time() - UNSETTLED_START_READ_SECONDS))
    for peer_id in sorted(with_children | unsettled):
        try:
            if sc.statement_tick_due(peer_id, every):
                _check_our_account(peer_id, debug_mode=debug_mode)
        except Exception as e:
            log.LOGGER(f"Error checking our account on {peer_id}: {e}")
    # Our account is also read where there is no child, now and then: a mark that grows
    # old would excuse movements that the peer hides (``statement_check.verify_chain``).
    # While the mark is old, a read is due every ``every`` ticks, as with a child: a read
    # that fails does not make the next tick read again.
    for peer_id in sorted(set(sc.peers_with_client()) - with_children - unsettled):
        try:
            if statement_check.stale_mark(sc.get_peer_statement_mark(peer_id).get("ts"),
                                          time.time()) and sc.statement_tick_due(peer_id, every):
                _check_our_account(peer_id, debug_mode=debug_mode)
        except Exception as e:
            log.LOGGER(f"Error checking our account on {peer_id}: {e}")

    for listed in delegated:
        # Under the lock of the deposit, with the row read again: what ``modify_deposit``
        # wrote since the tick began is part of it (``manager.delegation_lock``). The
        # tick does not wait for the lock: while ``modify_deposit`` holds it, this child
        # is charged at the next tick.
        lock = delegation_lock(listed.get('id'))
        if not lock.acquire(blocking=False):
            if debug_mode:
                log.LOGGER(f"The deposit of {listed.get('id')} is changing; charging it next tick.")
            continue
        try:
            row = sc.get_delegated_row(listed['token'])
            if row is None:
                continue
            peer_id, token, vmachine_id = row.get('peer_id'), row['token'], row.get('id')

            try:
                read = _read_child(row, debug_mode=debug_mode)
            except Exception as e:
                # A call that timed out says nothing about the instance.
                if not statement_check.unreachable(e) and is_peer_available(peer_id=peer_id):
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
                        stop_instance(token=vmachine_id, reason=dead_instances.PEER_STOPPED)
                    except Exception as stop_error:
                        log.LOGGER(f"Error stopping the delegated instance {vmachine_id}: {stop_error}")
                else:
                    # The time is charged without the peer: our price does not need it.
                    # What the peer charged waits for the next reading.
                    if debug_mode:
                        log.LOGGER(f"Peer {peer_id} is unreachable; charging the delegated "
                                   f"instance {vmachine_id} for its time only.")
                    settle_child(row, 0, _unmoved_state(row), debug_mode=debug_mode)
                continue

            charge_peer_mu, state = read
            settle_child(row, charge_peer_mu, state, debug_mode=debug_mode)
        finally:
            lock.release()
    forget_delegation_locks(listed.get('id') for listed in delegated)


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
            stop_instance(token=vmachine_id, reason=dead_instances.ACTIVITY_WINDOW)
        except Exception as e:
            log.LOGGER(f"Error stopping {vmachine_id} at closing time: {e}")


def close_orphaned_delegated_endpoints() -> None:
    """Close the local endpoints of delegated instances that are gone.

    A delegated instance stopped outside the daemon (``nodo kill`` runs in the CLI
    process) cannot close the daemon's listeners itself (issue #522). Never raises.
    """
    try:
        delegated_endpoints.close_orphaned()
    except Exception as e:
        log.LOGGER(f"Could not close orphaned delegated endpoints: {e}")


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


def _automatic_refill_of_gossip_peers_enabled() -> bool:
    """May the automatic refill also fund a peer only learned via gossip (#427)?

    Off unless the operator set it to exactly ``true``: a missing or malformed value is
    read as off, because the failure mode of guessing wrong the other way is paying
    every identity the network relays. Only narrows what ``_automatic_refill_enabled``
    already allows -- it never turns refills on by itself. Re-read every tick.
    """
    return env_manager.get("deposits.AUTOMATIC_REFILL_GOSSIP_PEERS", False) is True


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
                peer = fetch_peer_info(peer_channel(peer_id=peer_id), peer_id)
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

        # A peer some third party's gossip told us about is not one anybody here chose
        # (issue #427). Keypairs are free, so funding every relayed claim is a way to
        # drain this wallet a full deposit at a time. `nodo connect` or paying it by
        # hand is what makes it eligible; until then it is known, not funded -- unless
        # the operator opted in with `deposits.AUTOMATIC_REFILL_GOSSIP_PEERS`.
        if not _automatic_refill_of_gossip_peers_enabled() \
                and SQLConnection().peer_learned_via_gossip(peer_id=peer_id):
            if debug_mode:
                log.LOGGER(
                    f"Peer {peer_id} was learned via gossip; not funding it until it "
                    "is connected or paid by hand (deposits.AUTOMATIC_REFILL_GOSSIP_PEERS "
                    "is off)."
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
    top_up_unlimited_clients()


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


def reap_expired_suspensions() -> int:
    """Delete the suspended instances whose grace period is over and that were never topped up.

    A suspended instance (issue #594) keeps its disk for `instances.SUSPENDED_GRACE_SECONDS`
    so that its father can bring it back with a top-up. After that it dies with the cause
    it was suspended for, and its disk and address are released: `out_of_balance`, or the
    internal failure (`process_lost`, `guest_panic`) of one whose VM went down with less
    than a tick left, which was never revived for lack of MU. One that holds enough
    to pay a tick again is not touched, however long it has been suspended: it is waiting
    to be started, not to be deleted. One that holds something, but not that much, is
    still out of balance -- it would be suspended again on the next tick -- and goes too.
    Returns the number it deleted.
    """
    deleted = 0
    try:
        grace = dead_instances.suspended_grace_seconds()
        scarcity = None
        for row in sc.get_suspended_local_instances():
            if int(row.get("suspended_for_s") or 0) < grace:
                continue
            balance = int(row.get("balance_mu") or 0)
            if balance > 0:
                if scarcity is None:
                    scarcity = system_scarcity()
                if _can_pay_a_tick(row["id"], balance, scarcity):
                    continue
            try:
                reason = row.get("suspended_reason") or dead_instances.OUT_OF_BALANCE
                log.LOGGER(f"Grace period of suspended instance {row['id']} ({reason}) is over; "
                           f"deleting it.")
                if stop_instance(token=row["id"], reason=reason) is not None:
                    deleted += 1
            except Exception as e:
                log.LOGGER(f"Error deleting suspended instance {row['id']}: {e}")
    except Exception as e:
        log.LOGGER(f"Error in the suspended instance sweep: {e}")
    return deleted


DEAD_SWEEP_INTERVAL_S = 3600
_last_dead_sweep = 0.0


def sweep_dead_instances(force: bool = False) -> int:
    """Delete the dead instances that are past the retention limits.

    The limits are `instances.DEAD_RETENTION_DAYS` (age) and `instances.DEAD_MAX_ROWS`
    (count). The sweep runs at most once per hour unless ``force`` is true. It also
    removes the archive directories that have no row. Return the number of rows deleted.
    """
    global _last_dead_sweep
    now = time.monotonic()
    if not force and _last_dead_sweep and now - _last_dead_sweep < DEAD_SWEEP_INTERVAL_S:
        return 0
    _last_dead_sweep = now
    try:
        days = dead_instances.retention_days()
        max_rows = dead_instances.retention_max_rows()
        local_ids = sc.prune_dead_local_instances(max_age_days=days, max_rows=max_rows)
        delegated_tokens = sc.prune_dead_delegated_instances(max_age_days=days, max_rows=max_rows)
        dead_instances.remove_dirs(local_ids)
        # Keep the directories of the dead rows that remain. The rest have no row.
        kept = {row['id'] for row in sc.get_dead_local_instances()}
        dead_instances.sweep_orphan_dirs(kept)
        deleted = len(local_ids) + len(delegated_tokens)
        if deleted:
            log.LOGGER(f"Retention sweep deleted {deleted} dead instance(s).")
        return deleted
    except Exception as e:
        log.LOGGER(f"Error in the dead instance retention sweep: {e}")
        return 0


LEDGER_SWEEP_INTERVAL_S = 3600
_last_ledger_sweep = 0.0


def sweep_balance_movements(force: bool = False) -> int:
    """Delete the movements older than `ledger.RETENTION_DAYS` (#570).

    At most once per hour unless ``force`` is true. Returns the number of rows deleted.
    """
    global _last_ledger_sweep
    now = time.monotonic()
    if not force and _last_ledger_sweep and now - _last_ledger_sweep < LEDGER_SWEEP_INTERVAL_S:
        return 0
    _last_ledger_sweep = now
    try:
        deleted = sc.prune_balance_movements(max_age_days=ledger.retention_days())
        sc.prune_statement_faults(max_age_days=ledger.retention_days())
        sc.prune_start_attempts(max_age_days=ledger.retention_days())
        sc.prune_start_refs(max_age_days=ledger.retention_days())
        sc.prune_top_up_reservations(max_age_seconds=ledger.retention_days() * 86400)
        sc.sweep_failed_statement_reads(max_age_days=ledger.retention_days())
        if deleted:
            log.LOGGER(f"Retention sweep deleted {deleted} balance movement(s).")
        return deleted
    except Exception as e:
        log.LOGGER(f"Error in the balance movement retention sweep: {e}")
        return 0


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
    drain_wanted_inbox()
    if wanted_services:
        check_wanted_service(wanted_services.pop())  # IMPORTANT! If you want to manually execute this function via a command, you must ensure thread safety.
    maintain_vmachines(debug_mode=DEBUG_MODE())
    revive_suspended_instances()
    reap_expired_suspensions()
    sweep_dead_instances()
    sweep_balance_movements()
    maintain_delegated_instances(debug_mode=DEBUG_MODE())
    close_orphaned_delegated_endpoints()
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

    # Read what other nodes donated, off each chain, into SQLite (issue #282).
    # Self-gates to its own hourly interval and never raises. This is the only
    # place donations are read from a network: the balancer reads the rows.
    donations_tick()

    # And what the ledgers say about each peer we know (issue #353). Same shape, same
    # promise: self-gates to its own hourly interval, never raises, and is the only
    # place the reputation contract is read from a network -- the balancer reads rows.
    onchain_reputation_tick()

    # Bounded third-party peer discovery; each tick gates itself on live config.
    gossip_pull_tick()
    gossip_push_tick()

    # Tell known peers when this node's announcement changes -- its address (moving off
    # a LAN, a renewed dynamic public IP), the resources it offers, its rates, contracts
    # or proofs -- rather than waiting for one of them to re-fetch it on their own.
    # Self-gates to its own interval, never raises.
    announcement_change_tick()

    sleep(MANAGER_ITERATION_TIME)
    if DEBUG_MODE():
        log.LOGGER(f"Long interval count: {short_interval_count}/{SHORT_INTERVAL_COUNT}.")
    return short_interval_count + 1
