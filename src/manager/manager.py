from uuid import uuid4
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Generator, Tuple
import secrets

import grpc
from bee_rpc import client as bee

from src.manager.resources import IOBigData
from protos import celaut_pb2, celaut_pb2, celaut_pb2_grpc

from src.database.sql_connection import SQLConnection, is_peer_available
from src.tunneling import delegated_endpoints

from src.utils import logger as log
from src.utils import utils
from src.utils.config import ConfigManager
from src.utils.instance_names import normalize_instance_name, random_instance_name
from src.utils.utils import (
    from_gas_amount,
    to_gas_amount,
    generate_uris_by_peer_id
)
from src.utils.config import ConfigManager
from src.virtualizers.interface import remove_firewall_rule
from src.virtualizers.interface import kill
from src.virtualizers.interface import hotplug
from src.virtualizers.firewall import (
    TransportProtocol,
    resolve_slot_transport_protocols,
    serialize_transport_protocol,
)

env_manager = ConfigManager()

ALLOW_GAS_DEBT = env_manager.get("ALLOW_GAS_DEBT")
DATABASE_FILE = env_manager.get("DATABASE_FILE")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get("MIN_SLOTS_OPEN_PER_PEER")
DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT")
USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
MEMSWAP_FACTOR = env_manager.get("MEMSWAP_FACTOR")
def _parse_config_int(value, *, name: str) -> int:
    try:
        return int(Decimal(str(value)))
    except (ValueError, InvalidOperation) as e:
        raise ValueError(f"Invalid integer-like config value for {name}: {value}") from e


FEE_TRIAL_GAS_AMOUNT = _parse_config_int(env_manager.get("FREE_TRIAL_GAS_AMOUNT"), name="FREE_TRIAL_GAS_AMOUNT")
DEV_CLIENT_GAS_AMOUNT = _parse_config_int(env_manager.get("DEV_CLIENT_GAS_AMOUNT"), name="DEV_CLIENT_GAS_AMOUNT")
DEV_CLIENT_PREFIX = "dev-"
EXTERNAL_DEV_CLIENT_PREFIX = "dev-external-"
STANDARD_DEV_CLIENT_POOL_SIZE = int(env_manager.get("client.DEV_CLIENT_POOL_SIZE", 1))
DEV_EXTERNAL_CLIENT_POOL_SIZE = int(env_manager.get("client.DEV_EXTERNAL_CLIENT_POOL_SIZE", 1))

sc = SQLConnection()
_INSTANCE_NAME_RANDOM = secrets.SystemRandom()


def resolve_instance_token(reference: str, *, allow_uri_fallback: bool = False) -> Optional[str]:
    resolved = sc.resolve_local_instance_reference(reference=reference)
    if resolved:
        return resolved
    if allow_uri_fallback:
        return sc.get_local_instance_id_by_uri(uri=reference)
    return None


def reserve_instance_name(requested_name: Optional[str] = None) -> str:
    if requested_name:
        normalized = normalize_instance_name(requested_name)
        if sc.local_instance_name_exists(normalized):
            raise ValueError(f"Instance name '{normalized}' is already in use.")
        return normalized

    for _ in range(256):
        candidate = random_instance_name(_INSTANCE_NAME_RANDOM.randrange)
        if not sc.local_instance_name_exists(candidate):
            return candidate
    raise RuntimeError("Unable to generate a unique random instance name.")


def is_external_execute_client(client_id: str) -> bool:
    return str(client_id).startswith(EXTERNAL_DEV_CLIENT_PREFIX)


def _get_dev_clients_by_prefix(prefix: str) -> List[str]:
    if prefix == DEV_CLIENT_PREFIX:
        return [client_id for client_id in sc.get_dev_clients() if not is_external_execute_client(client_id)]
    return [client_id for client_id in sc.get_dev_clients() if str(client_id).startswith(prefix)]


def _get_client_gas_amount(client_id: str) -> Optional[int]:
    client_gas = sc.get_client_gas(client_id=client_id)
    if not client_gas:
        log.LOGGER(f"Client {client_id} has no readable gas entry. Skipping.")
        return None
    return client_gas[0]


def _target_dev_client_gas(gas_amount: int) -> int:
    return max(DEV_CLIENT_GAS_AMOUNT, int(gas_amount) + 1)


def _create_dev_client(prefix: str, gas_amount: Optional[int] = None) -> str:
    client_id = f"{prefix}{uuid4()}"
    sc.add_client(
        client_id=client_id,
        gas=_target_dev_client_gas(gas_amount or 0),
        last_usage=None,
    )
    return client_id


def _create_verified_dev_client(prefix: str, gas_amount: int) -> str:
    for _ in range(3):
        client_id = _create_dev_client(prefix, gas_amount=gas_amount)
        client_gas = _get_client_gas_amount(client_id=client_id)
        if client_gas is not None and client_gas > gas_amount:
            return client_id
        log.LOGGER(f"Dev client {client_id} was created but not readable. Retrying.")
    raise RuntimeError(f"No dev client available for prefix {prefix}.")


def _ensure_dev_client_pool(prefix: str, pool_size: int) -> List[str]:
    clients = _get_dev_clients_by_prefix(prefix)
    readable_clients: List[str] = []
    for client_id in clients:
        client_gas = _get_client_gas_amount(client_id=client_id)
        if client_gas is None:
            continue
        target_gas = _target_dev_client_gas(0)
        if client_gas < target_gas:
            sc.add_gas(client_id=client_id, gas=target_gas - client_gas)
        readable_clients.append(client_id)
    missing_clients = max(0, pool_size - len(readable_clients))
    for _ in range(missing_clients):
        log.LOGGER(f"Adds dev client for prefix {prefix}.")
        readable_clients.append(_create_verified_dev_client(prefix, gas_amount=0))
    return readable_clients


def ensure_dev_client_pools() -> None:
    _ensure_dev_client_pool(DEV_CLIENT_PREFIX, STANDARD_DEV_CLIENT_POOL_SIZE)
    _ensure_dev_client_pool(EXTERNAL_DEV_CLIENT_PREFIX, DEV_EXTERNAL_CLIENT_POOL_SIZE)


def _acquire_dev_client(prefix: str, pool_size: int, gas_amount: int) -> str:
    clients = _ensure_dev_client_pool(prefix, pool_size)

    for client_id in clients:
        client_gas = _get_client_gas_amount(client_id=client_id)
        if client_gas is not None and client_gas > gas_amount:
            return client_id

    if not clients:
        return _create_verified_dev_client(prefix, gas_amount=gas_amount)

    client_id = clients[0]
    current_gas = _get_client_gas_amount(client_id=client_id)
    if current_gas is None:
        return _create_verified_dev_client(prefix, gas_amount=gas_amount)

    target_gas = _target_dev_client_gas(gas_amount)
    if current_gas < target_gas:
        sc.add_gas(client_id=client_id, gas=target_gas - current_gas)
    return client_id


def get_dev_clients(gas_amount: int) -> Generator[str, None, None]:
    clients = _ensure_dev_client_pool(DEV_CLIENT_PREFIX, STANDARD_DEV_CLIENT_POOL_SIZE)
    for client_id in clients:
        client_gas = _get_client_gas_amount(client_id=client_id)
        if client_gas is not None and client_gas > gas_amount:
            yield client_id


def get_execute_client(gas_amount: int, external: bool = False) -> str:
    prefix = EXTERNAL_DEV_CLIENT_PREFIX if external else DEV_CLIENT_PREFIX
    pool_size = DEV_EXTERNAL_CLIENT_POOL_SIZE if external else STANDARD_DEV_CLIENT_POOL_SIZE
    return _acquire_dev_client(prefix, pool_size, gas_amount)
            
def add_reputation_proof(contract_ledger, peer_id) -> bool:
    from src.reputation_system.contracts.ergo.proof_validation import validate_contract_ledger as validate_ergo_reputation

    # Verify contract and ledger compatibility and ownership
    if not validate_ergo_reputation(contract_ledger, peer_id):
        log.LOGGER(f"Not supported reputation contract.")
        return False
    
    # Stores on DB
    return sc.add_reputation_proof(contract=contract_ledger, peer_id=peer_id)


def _peer_slot_transport_payloads(peer: celaut_pb2.Peer, peer_id: str) -> Dict[int, bytes]:
    payloads_by_port: Dict[int, bytes] = {}

    for api_slot in peer.instance.api.slot:
        protocol = resolve_slot_transport_protocols(
            api_slot,
            logger_fn=log.LOGGER,
            context=f"[PEER][{peer_id}]",
        )
        payloads_by_port[api_slot.port] = serialize_transport_protocol(protocol)

    return payloads_by_port

def _known_peer_id(instance: celaut_pb2.Instance) -> Optional[str]:
    """Resolve the id of an already-registered peer from the URIs it advertises.

    Legacy fallback path only: a signed ``Peer`` (public_key + valid signature) is
    identified by its public key directly (see :func:`_verified_peer_public_key`),
    with no need to look anything up by address.
    """
    from src.database.access_functions.peers import get_peer_id_by_ip

    for slot in instance.uri_slot:
        for uri in slot.uri:
            try:
                return get_peer_id_by_ip(ip=uri.ip)
            except (StopIteration, IndexError, TypeError):
                continue
    return None


def _peer_uris(instance: celaut_pb2.Instance) -> List[str]:
    return [f"{uri.ip}:{uri.port}" for slot in instance.uri_slot for uri in slot.uri]


def _verified_peer_public_key(peer: celaut_pb2.Peer) -> Optional[str]:
    """The sender's public key, if ``peer`` carries a signature that verifies against it.

    Issue #236: a node's identity is its public key, proven by a signature over the
    canonical encoding of (public_key, ts, seq, its own URIs) -- no interactive
    challenge needed. Returns None for a peer with no public_key/signature at all (a
    peer that predates this, or one with no identity mnemonic configured), so callers
    can fall back to the legacy address-based identity.
    """
    if not peer.public_key or not peer.signature:
        return None

    from src.reputation_system.node_identity import canonical_peer_payload, verify_peer_payload

    payload = canonical_peer_payload(
        peer.public_key, peer.ts, peer.seq, _peer_uris(peer.instance)
    )
    if not verify_peer_payload(peer.public_key, payload, peer.signature):
        log.LOGGER(f"Peer signature failed to verify for claimed public_key {peer.public_key}.")
        return None
    return peer.public_key


def _passes_anti_replay(peer_id: str, ts: int, seq: int) -> bool:
    """Reject a signed Peer message that is not strictly newer than the last accepted.

    Guards only against a downgrade to a stale address; the claim itself (this
    node's own public_key, freely signed by itself) is safe to accept from anyone
    who relays it, so there is no check beyond monotonicity.
    """
    last = sc.get_peer_last_ts_seq(peer_id=peer_id)
    if last is None:
        return True
    return (int(ts), int(seq)) > last


# Insert the instance if it does not exist, refresh it otherwise.
def add_peer_instance(peer: celaut_pb2.Peer) -> Optional[str]:
    verified_public_key = _verified_peer_public_key(peer)
    if verified_public_key:
        peer_id = verified_public_key
        if sc.peer_exists(peer_id=peer_id):
            if not _passes_anti_replay(peer_id, peer.ts, peer.seq):
                log.LOGGER(f"Peer {peer_id} sent a stale (ts, seq); ignoring the update.")
                return peer_id
            update_peer_instance(peer=peer, peer_id=peer_id)
            sc.set_peer_last_ts_seq(peer_id=peer_id, ts=peer.ts, seq=peer.seq)
            return peer_id
        # Falls through to the fresh-registration path below with peer_id already
        # resolved to the verified public key (no uuid4(), no address lookup).
    elif sc.instance_exists(peer.instance):
        # A peer we already know is re-introducing itself, unsigned (legacy). Dropping
        # the message here would freeze whatever it advertised the *first* time, so a
        # peer that had no payment contract back then (no wallet yet, ledger init
        # skipped, ...) would stay unpayable forever. Re-run the same registration path.
        peer_id = _known_peer_id(peer.instance)
        if not peer_id:
            log.LOGGER("Peer instance already exists but its id could not be resolved.")
            return None
        update_peer_instance(peer=peer, peer_id=peer_id)
        return peer_id
    else:
        peer_id = str(uuid4())

    protocol_stack: bytes = (
        peer.instance.api.slot[0].SerializeToString()
        if peer.instance.api.slot
        else b""
    )
    slot_transport_payloads = _peer_slot_transport_payloads(peer=peer, peer_id=peer_id)

    if not sc.add_peer(peer_id=peer_id, protocol_stack=protocol_stack):
        return None

    if verified_public_key:
        sc.set_peer_last_ts_seq(peer_id=peer_id, ts=peer.ts, seq=peer.seq)

    # Slots
    for slot in peer.instance.uri_slot:
        payload = slot_transport_payloads.get(slot.internal_port)
        if payload is None:
            log.LOGGER(
                f"[PEER][{peer_id}] Internal URI slot {slot.internal_port} not present in API slot declaration. Skipping."
            )
            continue
        if not payload:
            log.LOGGER(
                f"[PEER][{peer_id}] Internal URI slot {slot.internal_port} has no host-supported transports. Skipping."
            )
            continue
        sc.add_slot(slot=slot, peer_id=peer_id, transport_protocol=payload)

    # Contracts
    for gas_price in peer.instance.api.payment_contracts:
        log.LOGGER(f"Adding contract {gas_price.contract} for peer {peer_id}")
        try:
            sc.add_contract(contract=gas_price.contract, peer_id=peer_id, gas_price=from_gas_amount(gas_price.gas_amount))
        except Exception as e:
            log.LOGGER(f"Error adding contract {gas_price.contract} for peer {peer_id}: {e}")

    for contract in peer.reputation_proofs:
        log.LOGGER(f"Adding reputation proof {contract} for peer {peer_id}")
        try:
            if not add_reputation_proof(contract_ledger=contract, peer_id=peer_id):
                log.LOGGER(f"Controlled error to add reputation proof {contract} for peer {peer_id}")
                continue
        except Exception as e:
            log.LOGGER(f"Uncontrolled error adding reputation proof {contract} for peer {peer_id}: {e}")

    return peer_id

def update_peer_instance(peer: celaut_pb2.Peer, peer_id: str):
    log.LOGGER(f"Updating peer {peer_id}")
    # parsed_peer = json.loads(MessageToJson(peer))
    # It is assumed that protocol stack and metadata have not been modified.
    slot_transport_payloads = _peer_slot_transport_payloads(peer=peer, peer_id=peer_id)

    # Slots. add_slot upserts on (peer_id, internal_port) and merges each URI, so
    # re-registering a known peer accumulates its reachable addresses instead of
    # wiping them down to whatever it happened to advertise this time (issue #236).
    for slot in peer.instance.uri_slot:
        payload = slot_transport_payloads.get(slot.internal_port)
        if payload is None:
            log.LOGGER(
                f"[PEER][{peer_id}] Internal URI slot {slot.internal_port} not present in API slot declaration. Skipping."
            )
            continue
        if not payload:
            log.LOGGER(
                f"[PEER][{peer_id}] Internal URI slot {slot.internal_port} has no host-supported transports. Skipping."
            )
            continue
        sc.add_slot(slot=slot, peer_id=peer_id, transport_protocol=payload)

    # Contracts
    if not peer.instance.api.payment_contracts:
        log.LOGGER(f"Peer {peer_id} advertises no payment contract; it cannot be paid.")
    for gas_price in peer.instance.api.payment_contracts:
        log.LOGGER(f"Adding contract {gas_price.contract} for peer {peer_id}")
        try:
            sc.add_contract(contract=gas_price.contract, peer_id=peer_id, gas_price=from_gas_amount(gas_price.gas_amount))
        except Exception as e:
            # One malformed contract must not abort the rest of the refresh.
            log.LOGGER(f"Error adding contract {gas_price.contract} for peer {peer_id}: {e}")

    for contract_ledger in peer.reputation_proofs:
        if not add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id):
            continue

    log.LOGGER(f"Peer {peer_id} updated.")


def refresh_peer_instance(peer_id: str) -> bool:
    """Re-fetch a known peer's instance over ``GetPeerInfo`` and re-register it.

    Used when the locally-stored view of a peer is stale — most importantly when we
    hold no payment contract for it, since the peer may have started advertising one
    after the handshake that created its row.
    """
    uri = next(generate_uris_by_peer_id(peer_id=peer_id), "")
    if not uri:
        log.LOGGER(f"No known URI for peer {peer_id}; cannot refresh.")
        return False
    try:
        peer = next(bee.client_grpc(
            method=celaut_pb2_grpc.GatewayStub(
                grpc.insecure_channel(uri)
            ).GetPeerInfo,
            indices_parser=celaut_pb2.Peer,
            partitions_message_mode_parser=True
        ), None)
    except Exception as e:
        log.LOGGER(f"Could not fetch info for peer {peer_id}: {e}")
        return False

    if not peer:
        log.LOGGER(f"No peer info returned by {peer_id}.")
        return False

    update_peer_instance(peer=peer, peer_id=peer_id)
    return True

def get_internal_service_id_by_uri(uri: str) -> str:
    return sc.get_local_instance_id_by_uri(uri=uri)

def __refund_gas(
        gas: int = None,
        token: str = None,
        add_function=None,  # Lambda function if cache is not a dict of token:gas
) -> bool:
    try:
        add_function(gas)
    except Exception as e:
        log.LOGGER('Manager error: ' + str(e))
        return False
    return True


# Only can be executed once.
def __refund_gas_function_factory(
        gas: int = None,
        token: str = None,
        container: list = None,
        add_function=None
) -> lambda: None:
    if container is not None:
        container.append(
            lambda: __refund_gas(gas=gas, token=token, add_function=add_function)
        )


def increase_local_gas_for_client(client_id: str, amount: int) -> bool:
    log.LOGGER('Increase local gas for client ' + client_id + ' of ' + str(amount))
    if not sc.client_exists(client_id=client_id):
        raise Exception('Client ' + client_id + ' does not exists.')
    if not __refund_gas(
            gas=amount,
            add_function=lambda gas: sc.add_gas(client_id=client_id, gas=gas),
            token=client_id
    ):
        raise Exception('Manager error: cannot increase local gas for client ' + client_id + ' by ' + str(amount))
    return True


def spend_gas(
        id: str,
        gas_to_spend: int,
        refund_gas_function_container: list = None,
        debug_mode: bool=True
) -> bool:
    """
    Attempts to deduct gas from a client or container.
    Returns True if successful, False otherwise (with logging on failures).
    """
    gas_to_spend = int(gas_to_spend)
    try:
        is_client = sc.client_exists(client_id=id)
        # If the identifier corresponds to a client
        if is_client:
            client_data = sc.get_client_gas(client_id=id)
            if not client_data:
                log.LOGGER(f"No gas record found for client '{id}'.")
                return False

            actual_gas, last_usage, sci_not = client_data
            actual_gas = int(actual_gas)

            if actual_gas < gas_to_spend and not bool(ALLOW_GAS_DEBT):
                log.LOGGER(f"Insufficient gas for client '{id}': {sci_not} available, needed {log.ssformat(gas_to_spend)}.")
                return False

            if debug_mode: log.LOGGER(f"Reduce {log.ssformat(gas_to_spend)} gas for the client {id}")
            sc.reduce_gas(client_id=id, gas=gas_to_spend)

            __refund_gas_function_factory(
                gas=gas_to_spend,
                token=id,
                add_function=lambda gas: sc.add_gas(client_id=id, gas=gas),
                container=refund_gas_function_container
            )
            return True

        # If the identifier corresponds to a container (by ID or URI)
        else:
            is_id = sc.internal_instance_exists(id=id)
            if not is_id:
                resolved_id = resolve_instance_token(id, allow_uri_fallback=True)
                if not resolved_id:
                    return False
                id = resolved_id
                is_id = sc.internal_instance_exists(id=id)
                if not is_id:
                    log.LOGGER(f"Resolved container ID '{id}' does not exist.")
                    return False

            # Atomic read-check-write: a service tunnel bills the same container
            # from both relay directions at once, and a separate get/update would
            # let two threads read the same balance and lose one deduction.
            spent = sc.spend_container_gas(
                id=id, gas_to_spend=gas_to_spend, allow_debt=bool(ALLOW_GAS_DEBT)
            )
            if spent is None:
                log.LOGGER(f"Container '{id}' does not exist; cannot spend gas.")
                return False
            if spent is False:
                log.LOGGER(f"Insufficient gas for container '{id}': needed {log.ssformat(gas_to_spend)}.")
                return False
            if debug_mode: log.LOGGER(f"Container {id} spent {log.ssformat(gas_to_spend)} gas.")

            __refund_gas_function_factory(
                gas=gas_to_spend,
                add_function=lambda gas: sc.update_gas_to_container(id=id, gas=gas),
                token=id,
                container=refund_gas_function_container
            )
            return True

    except Exception as e:
        log.LOGGER(f"Manager error spending gas for '{id}': {e}")
        return False


def generate_client() -> celaut_pb2.Client:
    # No collisions expected.
    client_id = uuid4().hex
    sc.add_client(client_id=client_id, gas=FEE_TRIAL_GAS_AMOUNT, last_usage=None)
    log.LOGGER('New client created ' + client_id)
    return celaut_pb2.Client(
        client_id=client_id,
    )


def get_client_id_on_other_peer(peer_id: str) -> Optional[str]:
    """
    Retrieves or generates a client ID for a given peer. If the peer already has an associated client ID for our client,
    it returns that ID. If not, it checks if the peer is available. If the peer is available, it generates a new client ID,
    associates it with the peer, and returns the new client ID.

    Args:
        peer_id (str): The ID of the peer for which to retrieve or generate a client ID for our client.

    Returns:
        Optional[str]: The client ID associated with the peer for our client. Returns None if client ID generation or association fails.

    Raises:
        Exception: If the peer is not available (i.e., it does not have the minimum required open slots).

    Detailed Steps:
        1. Check if the peer already has an associated client ID for our client using `sc.get_peer_client`.
        2. If a client ID is found, return it.
        3. If no client ID is found, check if the peer is available using `is_peer_available`.
        4. If the peer is not available, log the unavailability and raise an exception.
        5. If the peer is available, generate a new client ID using `bee.client_grpc`.
        6. Log the generation of the new client ID.
        7. Attempt to associate the new client ID with the peer using `sc.add_external_client`.
        8. If the association is successful, return the new client ID.
        9. If the association fails, return None.
    """
    client_id = sc.get_peer_client(peer_id=peer_id)
    if client_id: return client_id
    if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
        raise Exception('Peer not available.')

    log.LOGGER('Generate new client for peer ' + peer_id)
    client_msg = next(bee.client_grpc(
        method=celaut_pb2_grpc.GatewayStub(
            grpc.insecure_channel(
                next(generate_uris_by_peer_id(peer_id=peer_id), "")
            )
        ).GenerateClient,
        indices_parser=celaut_pb2.Client,
        partitions_message_mode_parser=True
    ), "")
    if not client_msg:
        raise Exception("No client msg returned.")
    new_client_id = str(client_msg.client_id)
    if not sc.add_external_client(peer_id=peer_id, client_id=new_client_id):
        return  # If fails return None.

    return new_client_id


def default_initial_cost(
        father_id: str = None,
) -> int:
    log.LOGGER('Default cost for ' + (father_id if father_id else 'local'))
    return (int(
        sc.get_gas_amount_by_father_id(id=father_id) * DEFAULT_INITIAL_GAS_AMOUNT_FACTOR)
    ) if father_id and USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR else int(DEFAULT_INITIAL_GAS_AMOUNT)

def get_sysresources(id: str) -> celaut_pb2.ModifyServiceSystemResourcesOutput:
    sys_req = sc.get_sys_req(id=id)
    return celaut_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sys_req["mem_limit"],
            disk_space=sys_req["disk_space"],
        ),
        gas=to_gas_amount(
            gas_amount=sc.get_container_gas(id=id)
        )
    )


def stop_instance(token: str) -> Optional[int]:  # TODO Should be divided into two functions (for internal and for external), because part of it's use knows if is external or internal before call the function.
    token = resolve_instance_token(token) or token
    log.LOGGER('Kill service ' + token)
    father_id, serialized_instance = None, None
    reserved_mem_limit = 0
    
    if sc.internal_instance_exists(id=token):  # Is internal
        log.LOGGER(f"Token {token} is internal; let's stop it.")
        try:
            reserved_mem_limit = int(sc.get_sys_req(id=token)['mem_limit'] or 0)
        except Exception as e:
            log.LOGGER(f"Unable to read reserved memory for {token}: {e}")
        IOBigData().log_snapshot(
            context=f"stop-instance:before-kill token={token} reserved_mem_limit={reserved_mem_limit}"
        )
        
        kill(vmachine_id=token)
        
        father_id = sc.get_internal_father_id(id=token)
        serialized_instance = sc.get_internal_instance(id=token)
        
        try:
            refund = sc.get_container_gas(id=token)
            if reserved_mem_limit > 0:
                #  IOBigData().unlock_ram(ram_amount=reserved_mem_limit)
                IOBigData().log_snapshot(
                    context=f"stop-instance:after-unlock token={token} released_mem_limit={reserved_mem_limit}"
                )
            sc.purge_internal(id=token)
            
        except Exception as e:
            log.LOGGER('Error purging ' + token + ' ' + str(e))
            return None

    else:  # It's external
        log.LOGGER(f"Token {token} is external; let's stop it.")
        external_token = None
        try:
            external_token = sc.get_delegated_token_by_id(id=token)
            if not external_token:
                log.LOGGER(f"No external token for the token {token}")
                return None
            
            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            if not external_token:
                log.LOGGER(f"No peer for the token {external_token}")
                return None
            
            peer_uri = next(utils.generate_uris_by_peer_id(peer_id))
            if not external_token:
                log.LOGGER(f"No peer uri for the peer {peer_id}")
                return None
            
            refund = utils.from_gas_amount(
                next(bee.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer_uri)
                    ).StopService,
                        partitions_message_mode_parser=True,
                        indices_parser=celaut_pb2.ModifyGasDepositOutput,
                        input=celaut_pb2.TokenMessage(token=external_token)
                )).amount
            )
            father_id = sc.get_external_father_id(token=external_token)
            serialized_instance = sc.get_delegated_instance(token=external_token)

            # Drop the delegation record only after the peer confirmed the stop:
            # if StopService raised we must keep the row so the stop can be
            # retried and the (remote-computed) refund reconciled, rather than
            # orphaning an instance the peer may still be running.
            sc.purgue_delegated(token=external_token)

        except Exception as e:
            log.LOGGER('Error purging external instance with hashed token ' + token + ' ' + str(e))
            return None
        finally:
            # Local tunnel endpoints are our own listeners; tearing them down is
            # independent of the remote call and must happen even when it fails,
            # or the listener socket, its serving thread and the bound port leak
            # for the process lifetime. close() is idempotent.
            if external_token:
                delegated_endpoints.close(token=external_token)

    # Block the parent's access to the ports of the removed service.
    if sc.internal_instance_exists(id=father_id):  # Check if the father is an internal instance.
        try:
            instance = celaut_pb2.Instance()
            instance.ParseFromString(serialized_instance)
            protocols_by_port: Dict[int, Optional[TransportProtocol]] = {}
            for api_slot in instance.api.slot:
                protocols_by_port[api_slot.port] = resolve_slot_transport_protocols(
                    api_slot,
                    logger_fn=log.LOGGER,
                    context=f"[STOP][{father_id}]",
                )

            for uri_slot in instance.uri_slot:
                internal_port = uri_slot.internal_port
                protocol = protocols_by_port.get(internal_port)
                if not protocol:
                    log.LOGGER(
                        f"[STOP][{father_id}] No host-supported transports for internal slot {internal_port}. Skipping firewall cleanup."
                    )
                    continue
                for uri in uri_slot.uri:
                    if not remove_firewall_rule(
                        vmachine_id=father_id,
                        ip=uri.ip,
                        port=uri.port,
                        protocol=protocol,
                    ):
                        log.LOGGER(
                            f"Firewall remove_rule failed for parent instance {father_id} "
                            f"({uri.ip}:{uri.port}/{protocol.value})"
                        )
                        # TODO This should be controlled.
        except Exception as e:
            log.LOGGER(f"Exception removing rules for the father {father_id}")

    # __refound_gas() # TODO refound gas to parent.
    #  env variable could be used.
    return refund


# Modify Gas Deposit
def modify_gas_deposit(gas_amount: int, service_token: str) -> Tuple[bool, str]:
    service_token = resolve_instance_token(service_token) or service_token
    
    log.LOGGER(f"Modify {gas_amount} gas of the service {service_token}")
    
    is_internal = sc.internal_instance_exists(id=service_token)
    
    if is_internal:
        father_id = sc.get_internal_father_id(id=service_token)
    else:
        external_token = sc.get_delegated_token_by_id(id=service_token)
        if not external_token:
            log.LOGGER(f"ERROR: The service {service_token} is not a valid external service.")
            return False, 'Invalid external service token' 

        father_id = sc.get_external_father_id(token=external_token)
        
    if not father_id:
        log.LOGGER(f"ERROR: The service {service_token} (internal {is_internal})  doesn't have father.  This should never happen.")
        return False, 'No father id'
    
    # if gas_amount > father_amount: 
    #   return False, "The father does not have enough gas."    
    #   #  If it cannot, it will throw an exception later.
    
    if gas_amount > 0:
        log.LOGGER(f"Spend gas from father {father_id}")
        if not spend_gas(
                id=father_id,
                gas_to_spend=gas_amount,
                refund_gas_function_container=[]
        ):
            return False, 'Error spending gas'
    
    elif gas_amount < 0:
        # This should be a increase_gas() function, reverse to spend_gas()
        log.LOGGER(f"Add gas to father {father_id}")
        
        if sc.internal_instance_exists(id=father_id):
            _gas = sc.get_container_gas(id=father_id)
            _gas += abs(gas_amount)
            sc.update_gas_to_container(id=service_token, gas=_gas)
            
        elif sc.client_exists(client_id=father_id):
            sc.add_gas(client_id=father_id, gas=gas_amount)
        
        else:
            return False, f'ERROR: The father ID {father_id} is neither a client nor an internal service.'
            
        pass
    
    else:
        return True, '0 gas have no sense'
    
    if is_internal:
        current_gas = sc.get_container_gas(id=service_token)
        desired_amount = current_gas+gas_amount
        
        if desired_amount < 0:
            return False, "Negative amount have no sense"
        sc.update_gas_to_container(id=service_token, gas=desired_amount)
    
    else:
        try:
            external_token = sc.get_delegated_token_by_id(id=service_token)
            if not external_token:
                log.LOGGER(f"No external token for the token {external_token}")
                return False, "No external token found."

            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            if not peer_id:
                log.LOGGER(f"No peer for the token {external_token}")
                return False, "No peer found for the external service."
                
            _output = next(bee.client_grpc(
                method=celaut_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(utils.generate_uris_by_peer_id(peer_id))
                    )
                ).ModifyGasDeposit,
                partitions_message_mode_parser=True,
                indices_parser=celaut_pb2.ModifyGasDepositOutput,
                input=celaut_pb2.ModifyGasDepositInput(
                    gas_difference=utils.to_gas_amount(gas_amount),
                    service_token=external_token
                )
            ))
            return _output.success, _output.message
        except Exception as e:
            log.LOGGER(f"Exception on modify_gas_deposit for external service: {e}")
            return False, "Node error."
    
    return True, "Gas modified correctly"
