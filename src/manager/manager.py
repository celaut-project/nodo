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
    from_amount,
    to_amount,
    generate_uris_by_peer_id
)
from src.utils.config import ConfigManager
from src.utils.monetary import free_tier, format_mu
from src.virtualizers.interface import remove_firewall_rule
from src.virtualizers.interface import kill
from src.virtualizers.interface import hotplug
from src.virtualizers.interface import resolve_billable_resources
from src.virtualizers.firewall import (
    TransportProtocol,
    resolve_slot_transport_protocols,
)

env_manager = ConfigManager()

# Namespaced, matching where it actually lives in config.yaml and how rpc_tunnel refers
# to it. The bare name resolved to the same value only via ConfigManager's flat-key
# fallback, which searches every section -- fine until two sections hold the name.
ALLOW_DEBT = bool(env_manager.get("costs.ALLOW_DEBT", True))
DATABASE_FILE = env_manager.get("DATABASE_FILE")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get("MIN_SLOTS_OPEN_PER_PEER")
MEMSWAP_FACTOR = env_manager.get("MEMSWAP_FACTOR")

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


def _get_client_balance(client_id: str) -> Optional[int]:
    client_balance = sc.get_client_balance(client_id=client_id)
    if not client_balance:
        log.LOGGER(f"Client {client_id} has no readable balance entry. Skipping.")
        return None
    return client_balance[0]


def _target_dev_client_balance(amount_mu: int) -> int:
    """Dev clients are unmetered, so their balance only has to clear whatever the
    caller is about to spend.

    This used to be `max(DEV_CLIENT_GAS_AMOUNT, ...)` with DEV_CLIENT_GAS_AMOUNT set to
    1e256 -- a number chosen to mean "never runs out", which is a flag pretending to be
    an amount. `sc.add_client(unmetered=True)` says it directly, and the balance stays a
    figure a human can read.
    """
    return int(amount_mu) + 1


def _create_dev_client(prefix: str, amount_mu: Optional[int] = None) -> str:
    client_id = f"{prefix}{uuid4()}"
    sc.add_client(
        client_id=client_id,
        balance_mu=_target_dev_client_balance(amount_mu or 0),
        last_usage=None,
        unmetered=True,
    )
    return client_id


def _create_verified_dev_client(prefix: str, amount_mu: int) -> str:
    for _ in range(3):
        client_id = _create_dev_client(prefix, amount_mu=amount_mu)
        client_balance = _get_client_balance(client_id=client_id)
        if client_balance is not None and client_balance > amount_mu:
            return client_id
        log.LOGGER(f"Dev client {client_id} was created but not readable. Retrying.")
    raise RuntimeError(f"No dev client available for prefix {prefix}.")


def _ensure_dev_client_pool(prefix: str, pool_size: int) -> List[str]:
    clients = _get_dev_clients_by_prefix(prefix)
    readable_clients: List[str] = []
    for client_id in clients:
        client_balance = _get_client_balance(client_id=client_id)
        if client_balance is None:
            continue
        target_balance = _target_dev_client_balance(0)
        if client_balance < target_balance:
            sc.add_balance(client_id=client_id, balance_mu=target_balance - client_balance)
        readable_clients.append(client_id)
    missing_clients = max(0, pool_size - len(readable_clients))
    for _ in range(missing_clients):
        log.LOGGER(f"Adds dev client for prefix {prefix}.")
        readable_clients.append(_create_verified_dev_client(prefix, amount_mu=0))
    return readable_clients


def ensure_dev_client_pools() -> None:
    _ensure_dev_client_pool(DEV_CLIENT_PREFIX, STANDARD_DEV_CLIENT_POOL_SIZE)
    _ensure_dev_client_pool(EXTERNAL_DEV_CLIENT_PREFIX, DEV_EXTERNAL_CLIENT_POOL_SIZE)


def _acquire_dev_client(prefix: str, pool_size: int, amount_mu: int) -> str:
    clients = _ensure_dev_client_pool(prefix, pool_size)

    for client_id in clients:
        client_balance = _get_client_balance(client_id=client_id)
        if client_balance is not None and client_balance > amount_mu:
            return client_id

    if not clients:
        return _create_verified_dev_client(prefix, amount_mu=amount_mu)

    client_id = clients[0]
    current_balance = _get_client_balance(client_id=client_id)
    if current_balance is None:
        return _create_verified_dev_client(prefix, amount_mu=amount_mu)

    target_balance = _target_dev_client_balance(amount_mu)
    if current_balance < target_balance:
        sc.add_balance(client_id=client_id, balance_mu=target_balance - current_balance)
    return client_id


def get_dev_clients(amount_mu: int) -> Generator[str, None, None]:
    clients = _ensure_dev_client_pool(DEV_CLIENT_PREFIX, STANDARD_DEV_CLIENT_POOL_SIZE)
    for client_id in clients:
        client_balance = _get_client_balance(client_id=client_id)
        if client_balance is not None and client_balance > amount_mu:
            yield client_id


def get_execute_client(amount_mu: int, external: bool = False) -> str:
    prefix = EXTERNAL_DEV_CLIENT_PREFIX if external else DEV_CLIENT_PREFIX
    pool_size = DEV_EXTERNAL_CLIENT_POOL_SIZE if external else STANDARD_DEV_CLIENT_POOL_SIZE
    return _acquire_dev_client(prefix, pool_size, amount_mu)
            
def add_reputation_proof(contract_ledger, peer_id) -> bool:
    from src.reputation_system.contracts.ergo.proof_validation import validate_contract_ledger as validate_ergo_reputation

    # Verify contract and ledger compatibility and ownership
    if not validate_ergo_reputation(contract_ledger, peer_id):
        log.LOGGER(f"Not supported reputation contract.")
        return False
    
    # Stores on DB
    return sc.add_reputation_proof(contract=contract_ledger, peer_id=peer_id)


def _store_peer_uris(peer: celaut_pb2.Peer, peer_id: str) -> List[Tuple[str, int]]:
    """Persist every address ``peer`` announced, each with the transport it declares.

    ``resolve_slot_transport_protocols`` reads ``.transport.tags`` and ``.port``, both
    of which a ``Peer.Uri`` carries, so it applies unchanged. An address whose
    transport the host does not support is skipped rather than stored: storing it
    would hand every later reader an endpoint it cannot speak to.

    Returns the addresses actually stored, so a caller pruning superseded ones keeps
    exactly those and not the ones it just refused: an address skipped here but left
    in ``keep`` would survive as a stale row carrying its previous transport.
    """
    stored: List[Tuple[str, int]] = []
    for uri in peer.uri:
        try:
            protocol = resolve_slot_transport_protocols(
                uri,
                logger_fn=log.LOGGER,
                context=f"[PEER][{peer_id}]",
            )
        except ValueError as e:
            log.LOGGER(f"[PEER][{peer_id}] Ignoring {uri.ip}:{uri.port}: {e}")
            continue
        if not protocol:
            log.LOGGER(
                f"[PEER][{peer_id}] Address {uri.ip}:{uri.port} declares no "
                "host-supported transport. Skipping."
            )
            continue
        sc.add_peer_uri(uri=uri, peer_id=peer_id, transport=protocol.value)
        stored.append((uri.ip, uri.port))
    return stored


def _peer_advertisement(peer: celaut_pb2.Peer) -> bytes:
    """The bytes stored in ``peer.advertisement``: the message exactly as it arrived.

    Kept verbatim rather than rebuilt from the stored columns so the signature it
    carries stays verifiable -- ``submit_to_ledger`` republishes it on-chain, where a
    reader can check it against the peer's public key without contacting the peer.
    """
    return peer.SerializeToString()


def _known_peer_id(uris) -> Optional[str]:
    """Resolve the id of an already-registered peer from the URIs it advertises.

    Legacy fallback path only: a signed ``Peer`` (public_key + valid signature) is
    identified by its public key directly (see :func:`_verified_peer_public_key`),
    with no need to look anything up by address.
    """
    from src.database.access_functions.peers import get_peer_id_by_ip

    for uri in uris:
        try:
            # Match ip AND port: a bare-IP match is ambiguous now that several
            # unrelated peers can legitimately share a private address.
            return get_peer_id_by_ip(ip=uri.ip, port=uri.port)
        except (StopIteration, IndexError, TypeError):
            continue
    return None


def _verified_peer_public_key(peer: celaut_pb2.Peer) -> Optional[str]:
    """The sender's public key, if ``peer`` carries a signature that verifies against it.

    Issue #236: a node's identity is its public key, proven by a signature over
    (public_key, ts, a digest of its advertised api and URIs) -- no interactive
    challenge needed. The digest covers the advertised payment contracts and API
    slots, not just the addresses, so a relayed message cannot have its payment
    contract swapped out and still verify.

    Returns None for a peer with no public_key/signature at all (a peer that predates
    this, or one with no identity mnemonic configured), so callers can fall back to
    the legacy address-based identity, and None for a non-canonical public key.
    """
    if not peer.public_key or not peer.signature:
        return None

    from src.reputation_system.node_identity import (
        canonical_peer_content_digest,
        canonical_peer_payload,
        normalize_public_key_hex,
        verify_peer_payload,
    )

    public_key = normalize_public_key_hex(peer.public_key)
    if public_key is None or public_key != peer.public_key:
        # Not a canonical 66-char lowercase hex key. Refusing rather than normalizing
        # keeps one spelling per identity: bytes.fromhex would happily accept "02AB…"
        # or "02 ab…", each of which would otherwise become its own peer row.
        log.LOGGER(f"Peer announced a non-canonical public_key {peer.public_key!r}; ignoring it.")
        return None

    payload = canonical_peer_payload(
        public_key,
        peer.ts,
        canonical_peer_content_digest(peer),
    )
    if not verify_peer_payload(public_key, payload, peer.signature):
        log.LOGGER(f"Peer signature failed to verify for claimed public_key {public_key}.")
        return None
    return public_key


def _passes_anti_replay(peer_id: str, ts: int) -> bool:
    """Reject a signed Peer message that is not strictly newer than the last accepted.

    Guards only against a downgrade to a stale address; the claim itself (this
    node's own public_key, freely signed by itself) is safe to accept from anyone
    who relays it, so there is no check beyond monotonicity.
    """
    last_ts = sc.get_peer_last_ts(peer_id=peer_id)
    if last_ts is None:
        return True
    return int(ts) > last_ts


# Insert the instance if it does not exist, refresh it otherwise.
def add_peer_instance(peer: celaut_pb2.Peer) -> Optional[str]:
    verified_public_key = _verified_peer_public_key(peer)
    if verified_public_key:
        peer_id = verified_public_key
        if not sc.peer_exists(peer_id=peer_id):
            # Migration (issue #236): this peer may already be here under the random
            # uuid4 id it got before node identity existed. Adopt that row instead of
            # starting a second one, or its balance, external client id, contracts and
            # reputation would be stranded on a record nothing references again.
            legacy_id = _known_peer_id(peer.uri)
            if legacy_id and legacy_id != peer_id and sc.rekey_peer(legacy_id, peer_id):
                log.LOGGER(f"Adopted legacy peer {legacy_id} as its public key {peer_id}.")

        if sc.peer_exists(peer_id=peer_id):
            if not _passes_anti_replay(peer_id, peer.ts):
                log.LOGGER(f"Peer {peer_id} sent a stale ts; ignoring the update.")
                return peer_id
            stored = update_peer_instance(peer=peer, peer_id=peer_id)
            # Only a verified, strictly-newer announcement may drop addresses, so a
            # replayed or unsigned message can never prune a peer's real ones.
            sc.prune_peer_uris(peer_id=peer_id, keep=stored)
            sc.set_peer_last_ts(peer_id=peer_id, ts=peer.ts)
            return peer_id
        # Falls through to the fresh-registration path below with peer_id already
        # resolved to the verified public key (no uuid4(), no address lookup).
    elif sc.peer_uris_exist(peer.uri):
        # A peer we already know is re-introducing itself, unsigned (legacy). Dropping
        # the message here would freeze whatever it advertised the *first* time, so a
        # peer that had no payment contract back then (no wallet yet, ledger init
        # skipped, ...) would stay unpayable forever. Re-run the same registration path.
        peer_id = _known_peer_id(peer.uri)
        if not peer_id:
            log.LOGGER("Peer instance already exists but its id could not be resolved.")
            return None
        from src.reputation_system.node_identity import normalize_public_key_hex

        if normalize_public_key_hex(peer_id) is not None:
            # The address resolved to a peer that already proved an identity, and this
            # message did not sign for it. Anyone can reach that address -- it is
            # published by GetPeerInfo and on-chain -- so accepting this would let a
            # stranger rewrite a verified peer's advertisement and payment contracts
            # just by naming its IP. Same rule accept_peer_refresh applies.
            log.LOGGER(
                f"Refusing an unsigned announcement for {peer_id}: that address belongs "
                "to a peer with an identity, and this message is not signed by it."
            )
            return peer_id
        update_peer_instance(peer=peer, peer_id=peer_id)
        return peer_id
    else:
        peer_id = str(uuid4())

    if not sc.add_peer(peer_id=peer_id, advertisement=_peer_advertisement(peer)):
        return None

    if verified_public_key:
        sc.set_peer_last_ts(peer_id=peer_id, ts=peer.ts)

    # Addresses
    _store_peer_uris(peer=peer, peer_id=peer_id)

    # Contracts
    for rate in peer.payment_contracts:
        log.LOGGER(f"Adding contract {rate.contract} for peer {peer_id}")
        try:
            sc.add_contract(contract=rate.contract, peer_id=peer_id, mu_per_unit=from_amount(rate.mu_per_unit))
        except Exception as e:
            log.LOGGER(f"Error adding contract {rate.contract} for peer {peer_id}: {e}")

    for contract in peer.reputation_proofs:
        log.LOGGER(f"Adding reputation proof {contract} for peer {peer_id}")
        try:
            if not add_reputation_proof(contract_ledger=contract, peer_id=peer_id):
                log.LOGGER(f"Controlled error to add reputation proof {contract} for peer {peer_id}")
                continue
        except Exception as e:
            log.LOGGER(f"Uncontrolled error adding reputation proof {contract} for peer {peer_id}: {e}")

    return peer_id

def update_peer_instance(peer: celaut_pb2.Peer, peer_id: str) -> List[Tuple[str, int]]:
    """Refresh a known peer; returns the addresses actually stored (see
    :func:`_store_peer_uris`), which is what a caller may then prune down to."""
    log.LOGGER(f"Updating peer {peer_id}")

    sc.set_peer_advertisement(peer_id=peer_id, advertisement=_peer_advertisement(peer))

    # Addresses. add_peer_uri upserts on (peer_id, ip, port), so re-registering a known
    # peer accumulates its reachable addresses instead of wiping them down to whatever
    # it happened to advertise this time (issue #236).
    stored = _store_peer_uris(peer=peer, peer_id=peer_id)

    # Contracts
    if not peer.payment_contracts:
        log.LOGGER(f"Peer {peer_id} advertises no payment contract; it cannot be paid.")
    for rate in peer.payment_contracts:
        log.LOGGER(f"Adding contract {rate.contract} for peer {peer_id}")
        try:
            sc.add_contract(contract=rate.contract, peer_id=peer_id, mu_per_unit=from_amount(rate.mu_per_unit))
        except Exception as e:
            # One malformed contract must not abort the rest of the refresh.
            log.LOGGER(f"Error adding contract {rate.contract} for peer {peer_id}: {e}")

    for contract_ledger in peer.reputation_proofs:
        if not add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id):
            continue

    log.LOGGER(f"Peer {peer_id} updated.")
    return stored


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

    return accept_peer_refresh(peer=peer, peer_id=peer_id)


def accept_peer_refresh(peer: celaut_pb2.Peer, peer_id: str) -> bool:
    """Store a ``GetPeerInfo`` response we solicited from ``peer_id``, if it is genuinely theirs.

    Whoever answers at a stored address is not necessarily the peer we meant to reach:
    the channel is plain ``grpc.insecure_channel`` with no TLS, and the address may have
    been reassigned by the ISP. This path feeds ``payment_contracts`` straight into the
    DB and runs immediately before ``pay`` sends money, so it must apply the same
    signature check as an inbound IntroducePeer rather than trusting the response.

    A peer whose id is already a public key MUST present a matching, valid signature.
    A legacy uuid-keyed peer has no key to check against, so it is accepted as before
    (no worse than the previous behaviour) until it re-handshakes and gets adopted.
    """
    verified_public_key = _verified_peer_public_key(peer)

    from src.reputation_system.node_identity import normalize_public_key_hex

    if normalize_public_key_hex(peer_id) is not None:
        # peer_id is an identity key: nothing but a signature from that exact key will do.
        if verified_public_key != peer_id:
            log.LOGGER(
                f"Refusing refresh for peer {peer_id}: response was not signed by that identity."
            )
            return False
        if not _passes_anti_replay(peer_id, peer.ts):
            log.LOGGER(f"Refusing refresh for peer {peer_id}: stale ts.")
            return False

    stored = update_peer_instance(peer=peer, peer_id=peer_id)

    if verified_public_key == peer_id:
        sc.prune_peer_uris(peer_id=peer_id, keep=stored)
        sc.set_peer_last_ts(peer_id=peer_id, ts=peer.ts)
    return True

def get_internal_service_id_by_uri(uri: str) -> str:
    return sc.get_local_instance_id_by_uri(uri=uri)

def __refund(
        amount_mu: int = None,
        token: str = None,
        add_function=None,  # Lambda if the cache is not a dict of token:balance
) -> bool:
    try:
        add_function(amount_mu)
    except Exception as e:
        log.LOGGER('Manager error: ' + str(e))
        return False
    return True


# Only can be executed once.
def __refund_function_factory(
        amount_mu: int = None,
        token: str = None,
        container: list = None,
        add_function=None
) -> lambda: None:
    if container is not None:
        container.append(
            lambda: __refund(amount_mu=amount_mu, token=token, add_function=add_function)
        )


def increase_local_balance_for_client(client_id: str, amount_mu: int) -> bool:
    log.LOGGER(f"Credit client {client_id} with {format_mu(amount_mu)}")
    if not sc.client_exists(client_id=client_id):
        raise Exception('Client ' + client_id + ' does not exists.')
    if not __refund(
            amount_mu=amount_mu,
            add_function=lambda amount: sc.add_balance(client_id=client_id, balance_mu=amount),
            token=client_id
    ):
        raise Exception(f"Manager error: cannot credit client {client_id} with {format_mu(amount_mu)}")
    return True


def spend_mu(
        id: str,
        amount_mu: int,
        refund_function_container: list = None,
        debug_mode: bool=True
) -> bool:
    """
    Attempts to deduct MU from a client or instance.
    Returns True if successful, False otherwise (with logging on failures).
    """
    amount_mu = int(amount_mu)
    try:
        is_client = sc.client_exists(client_id=id)
        # If the identifier corresponds to a client
        if is_client:
            client_data = sc.get_client_balance(client_id=id)
            if not client_data:
                log.LOGGER(f"No balance record found for client '{id}'.")
                return False

            balance, last_usage, _ = client_data
            balance = int(balance)

            if balance < amount_mu and not ALLOW_DEBT:
                log.LOGGER(
                    f"Insufficient balance for client '{id}': {format_mu(balance)} available, "
                    f"needed {format_mu(amount_mu)}."
                )
                return False

            if debug_mode:
                log.LOGGER(f"Charging client {id} {format_mu(amount_mu)}")
            sc.reduce_balance(client_id=id, balance_mu=amount_mu)

            __refund_function_factory(
                amount_mu=amount_mu,
                token=id,
                add_function=lambda amount: sc.add_balance(client_id=id, balance_mu=amount),
                container=refund_function_container
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
            spent = sc.spend_instance_balance(
                id=id, amount_mu=amount_mu, allow_debt=ALLOW_DEBT
            )
            if spent is None:
                log.LOGGER(f"Container '{id}' does not exist; cannot charge it.")
                return False
            if spent is False:
                log.LOGGER(
                    f"Insufficient balance for container '{id}': needed {format_mu(amount_mu)}."
                )
                return False
            if debug_mode:
                log.LOGGER(f"Container {id} charged {format_mu(amount_mu)}.")

            __refund_function_factory(
                amount_mu=amount_mu,
                add_function=lambda amount: sc.update_instance_balance(id=id, balance_mu=amount),
                token=id,
                container=refund_function_container
            )
            return True

    except Exception as e:
        log.LOGGER(f"Manager error charging '{id}': {e}")
        return False


def generate_client() -> celaut_pb2.Client:
    # No collisions expected.
    client_id = uuid4().hex
    sc.add_client(client_id=client_id, balance_mu=free_tier().credit_mu_per_new_client, last_usage=None)
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


def default_initial_balance(
    system_resources: celaut_pb2.Sysresources = None,
    service_hash: Optional[str] = None,
) -> int:
    """MU to fund a new instance with when nobody asked for a specific amount.

    Derived rather than configured: it is what the requested resources actually cost for
    `deposits.INITIAL_RUNTIME_HOURS`. The flat `DEFAULT_INITIAL_GAS_AMOUNT` this replaces
    funded a 128 MiB instance and an 8 GiB one identically, which meant it was either
    far too much for one or far too little for the other.

    Priced for what the instance will hold rather than for what its manifest asks: the
    ticks that spend this balance charge the resolved row, so the balance is computed
    from those same figures or it funds fewer hours than INITIAL_RUNTIME_HOURS.
    `service_hash`, when known, prices an already-built service's real rootfs image
    instead of the floor.
    """
    hours = float(env_manager.get("deposits.INITIAL_RUNTIME_HOURS", 1.0))
    if hours <= 0 or system_resources is None:
        return 0
    from src.utils.cost_functions.execution_cost import maintenance_charge_mu

    return maintenance_charge_mu(
        system_resources=resolve_billable_resources(system_resources, service_hash),
        seconds=hours * 3600,
    )

def get_sysresources(id: str) -> celaut_pb2.ModifyServiceSystemResourcesOutput:
    """What an instance holds and what it has left to spend.

    All four resource fields: this is the reply to ModifyServiceSystemResources, so a
    client that just resized an instance reads back its real shape. Omitting the CFS pair
    reported every instance as having no CPU at all.
    """
    sys_req = sc.get_sys_req(id=id)
    return celaut_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sys_req["mem_limit"] or 0,
            disk_space=sys_req["disk_space"] or 0,
            cpu_period=sys_req["cpu_period"] or 0,
            cpu_quota=sys_req["cpu_quota"] or 0,
        ),
        balance=to_amount(sc.get_instance_balance(id=id))
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
            refund = sc.get_instance_balance(id=token)
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
            
            refund = utils.from_amount(
                next(bee.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer_uri)
                    ).StopService,
                        partitions_message_mode_parser=True,
                        # StopService answers with a Refund, whose field is `amount`.
                        # This parsed it as a deposit-modification output, which has no
                        # such field -- pre-existing, surfaced by the rename.
                        indices_parser=celaut_pb2.Refund,
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

    # TODO refund the remaining balance to the parent.
    #  env variable could be used.
    return refund


# Modify an instance's deposit, in MU.
def modify_deposit(amount_mu: int, service_token: str) -> Tuple[bool, str]:
    service_token = resolve_instance_token(service_token) or service_token

    log.LOGGER(f"Modify deposit of {service_token} by {format_mu(abs(amount_mu))}")

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

    if amount_mu > 0:
        log.LOGGER(f"Charge father {father_id}")
        if not spend_mu(
                id=father_id,
                amount_mu=amount_mu,
                refund_function_container=[]
        ):
            return False, 'Error charging the father'

    elif amount_mu < 0:
        # The reverse of spend_mu(): what the instance gives back goes to its father.
        log.LOGGER(f"Credit father {father_id}")

        if sc.internal_instance_exists(id=father_id):
            father_balance = sc.get_instance_balance(id=father_id) + abs(amount_mu)
            sc.update_instance_balance(id=father_id, balance_mu=father_balance)

        elif sc.client_exists(client_id=father_id):
            sc.add_balance(client_id=father_id, balance_mu=abs(amount_mu))

        else:
            return False, f'ERROR: The father ID {father_id} is neither a client nor an internal service.'

    else:
        return True, 'Nothing to modify'

    if is_internal:
        desired_amount = sc.get_instance_balance(id=service_token) + amount_mu

        if desired_amount < 0:
            return False, "Negative amount have no sense"
        sc.update_instance_balance(id=service_token, balance_mu=desired_amount)

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
                ).ModifyDeposit,
                partitions_message_mode_parser=True,
                indices_parser=celaut_pb2.ModifyDepositOutput,
                input=celaut_pb2.ModifyDepositInput(
                    difference=utils.to_amount(amount_mu),
                    service_token=external_token
                )
            ))
            return _output.success, _output.message
        except Exception as e:
            log.LOGGER(f"Exception on modify_deposit for external service: {e}")
            return False, "Node error."

    return True, "Deposit modified correctly"
