import collections
import os
import socket
import threading
import time
import typing
import ipaddress
from decimal import Decimal
from typing import Generator, Optional

import netifaces as ni
from bee_rpc.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME as WITHOUT_BLOCKS_FILE_NAME  # <-- Esto es engañoso, porque el nombre del archivo es "without_block_pointers" y no "without_blocks".  Pero realmente el archivo es el objeto con punteros en lugar de bloques directamente.
from bee_rpc.client import Dir

from protos import celaut_pb2 as celaut
from protos import celaut_pb2
from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.manager.resources import mem_manager
from src.utils import logger as log
from src.utils.verify import get_service_hex_main_hash
from src.utils.config import ConfigManager

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
WAIT_FOR_UNLOCK_MEMORY = env_manager.get("builder.WAIT_FOR_UNLOCK_MEMORY")

def read_file(filename) -> bytes:
    def generator(file):
        with open(file, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                yield chunk

    return b''.join([b for b in generator(file=filename)])


def get_grpc_uri(instance: celaut.Instance) -> celaut.Instance.Uri:
    for slot in instance.api.slot:
        # if 'grpc' in slot.transport_protocol and 'http2' in slot.transport_protocol: # TODO
        # If the protobuf lib. supported map for this message it could be O(n).
        for uri_slot in instance.uri_slot:
            if uri_slot.internal_port == slot.port:
                return uri_slot.uri[0]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))


def service_hashes(
        hashes: typing.List[celaut_pb2.Metadata.HashTag.Hash]
) -> Generator[celaut.Metadata.HashTag.Hash, None, None]:
    for _hash in hashes:
        yield _hash


def read_service_from_disk(service_hash: str) -> Optional[celaut.Service]:
    log.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    filename: str = os.path.join(REGISTRY, service_hash)
    if not os.path.exists(filename):
        return None

    if os.path.isdir(filename):
        filename = filename + '/' + WITHOUT_BLOCKS_FILE_NAME
    try:
        mem_size = 2 * os.path.getsize(filename)
        log.LOGGER(f"Wait to unlock memory {mem_size}")
        with mem_manager(mem_size, timeout=WAIT_FOR_UNLOCK_MEMORY) as iolock:
            service = celaut.Service()
            service.ParseFromString(read_file(filename=filename))
            log.LOGGER(f"Service {service_hash} loaded.")
            return service
    except TimeoutError:
        log.LOGGER(
            f"Timed out after {WAIT_FOR_UNLOCK_MEMORY}s waiting to unlock memory for service {service_hash}."
        )
        return None
    except (IOError, FileNotFoundError):
        log.LOGGER('The service was not on registry.')
        return None


def read_metadata_from_disk(service_hash: str) -> Optional[celaut.Metadata]:
    filename: str = os.path.join(METADATA_REGISTRY, service_hash)
    if not os.path.exists(filename):
        return None

    try:
        metadata = celaut.Metadata()
        metadata.ParseFromString(read_file(filename=filename))
        return metadata
    except (IOError, FileNotFoundError):
        log.LOGGER('The metadata was not on registry.')
        return None


def service_extended(
        metadata: celaut.Metadata,
        config: typing.Optional[celaut_pb2.Configuration] = None,
        send_only_hashes: typing.Optional[bool] = False,
        client_id: typing.Optional[str] = None,
        recursion_guard_token: typing.Optional[str] = None
) -> Generator[object, None, None]:
    # 1
    if client_id:
        yield celaut_pb2.Client(
            client_id=client_id
        )

    # 2
    if recursion_guard_token:
        yield celaut_pb2.RecursionGuard(
            token=recursion_guard_token
        )

    # 3
    if config:
        yield config

    # 4
    yield from metadata.hashtag.hash

    if not send_only_hashes:
        # 5
        yield metadata

        # 6
        yield Dir(
            dir=REGISTRY + get_service_hex_main_hash(metadata=metadata),
            _type=celaut.Service
        )

get_only_the_ip_from_context = lambda context_peer: __get_only_the_ip_from_context_method(context_peer)


def __get_only_the_ip_from_context_method(context_peer: str) -> str:
    try:
        ipv = context_peer.split(':')[0]
        if ipv in ('ipv4', 'ipv6'):
            ip = context_peer[5:-1 * (len(context_peer.split(':')[
                                              -1]) + 1)]  # The format is 'ipv4:49.123.106.100:4442', we don't want 'ipv4:' nor the port.
            return ip[1:-1] if ipv == 'ipv6' else ip
    except Exception as e:
        raise Exception('Error getting the ip from the context: ' + str(e))


def _clean_ip_value(value: str) -> str:
    return str(value).split("%", 1)[0]


def _extract_direction_host(direction: str) -> str:
    normalized = str(direction or "").replace("http://", "").replace("https://", "").strip()
    if not normalized:
        return ""

    if normalized.startswith("[") and "]" in normalized:
        return _clean_ip_value(normalized[1:normalized.index("]")])

    # IPv6 literals may contain multiple ":" and optional zone ids. Only split host:port for clear IPv4/hostnames.
    if normalized.count(":") == 1:
        return _clean_ip_value(normalized.split(":", 1)[0])

    return _clean_ip_value(normalized)


def _is_link_local_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(_clean_ip_value(ip)).is_link_local
    except ValueError:
        return False


_VIRTUAL_INTERFACE_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "zt",
    "tailscale",
    "tun",
    "tap",
    "wg",
    "vmnet",
    "vboxnet",
)


def is_virtual_interface(interface: str) -> bool:
    """True for container/VPN/VM interfaces (docker0, br-*, veth*, tailscale0, ...).

    Their addresses are real on the host but not a way anyone else reaches this
    node through, so callers enumerating interfaces to advertise or prioritise a
    LAN address should skip them -- otherwise a Docker bridge IP (e.g.
    172.17.0.1) gets treated the same as the actual LAN interface.
    """
    return (interface or "").strip().lower().startswith(_VIRTUAL_INTERFACE_PREFIXES)


def get_local_ip_from_network(network: str, *, allow_link_local: bool = True) -> str:
    addresses = ni.ifaddresses(network)

    ipv4_addresses = addresses.get(ni.AF_INET, [])
    if ipv4_addresses:
        for address in ipv4_addresses:
            candidate = _clean_ip_value(address.get("addr", ""))
            if candidate and (allow_link_local or not _is_link_local_ip(candidate)):
                return candidate

    ipv6_addresses = addresses.get(ni.AF_INET6, [])
    fallback_link_local_ipv6 = ""
    for address in ipv6_addresses:
        candidate = _clean_ip_value(address.get("addr", ""))
        if not candidate:
            continue
        if not _is_link_local_ip(candidate):
            return candidate
        if allow_link_local and not fallback_link_local_ipv6:
            fallback_link_local_ipv6 = candidate

    if fallback_link_local_ipv6:
        return fallback_link_local_ipv6

    raise KeyError(
        f"No usable IPv4/IPv6 address found for interface {network}"
        + ("" if allow_link_local else " without link-local addresses")
    )

longestSublistFinder = lambda string1, string2, split: split.join(
    [a for a in string1.split(split) for b in string2.split(split) if a == b]) + split


def __address_in_network(ip_or_uri, net) -> bool:
    #  Return if the ip network portion (addr and broadcast common) is in the ip.
    return (
                   longestSublistFinder(
                       string1=ni.ifaddresses(net)[ni.AF_INET][0]['addr'],
                       string2=ni.ifaddresses(net)[ni.AF_INET][0]['broadcast'],
                       split='.'
                   ) or
                   longestSublistFinder(
                       string1=ni.ifaddresses(net)[ni.AF_INET6][0]['addr'],
                       string2=ni.ifaddresses(net)[ni.AF_INET6][0]['broadcast'],
                       split='::'
                   )
           ) in ip_or_uri \
        if net != 'lo' else \
        ni.ifaddresses(net)[ni.AF_INET][0]['addr'] == ip_or_uri or \
        ni.ifaddresses(net)[ni.AF_INET6][0]['addr'] == ip_or_uri


def get_network_name(direction: str) -> Optional[str]:
    """
    Get the network name for a given direction. If the direction contains a port, it will be removed.

    Args:
        direction (str): The direction to get the network name for.

    Returns:
        Optional[str]: The name of one of our own interfaces whose subnet contains
        ``direction``. ``"localhost"`` specifically means ``direction`` is our own
        loopback (``0.0.0.0``/``::1``). ``None`` means ``direction`` is not on any
        network we are on -- e.g. a real peer reached over the internet -- which is
        a different situation from loopback and must not be treated as if it were
        one (there is no interface actually named "localhost" to resolve an IP from).

    Raises:
        Exception: If there's an error processing the network interfaces
    """
    direction = _extract_direction_host(direction)

    # If is localhost
    if "::1" in direction or '0.0.0.0' == direction:
        return "localhost"

    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    try:
        for network in ni.interfaces():
            try:
                if __address_in_network(ip_or_uri=direction, net=network):
                    return network
            except KeyError:
                continue

        # direction does not belong to any network we are on.
        return None

    except Exception as e:
        raise Exception('Error getting the network name: ' + str(e))


def to_amount(amount_mu) -> celaut_pb2.Amount:
    # Normalize through Decimal before stringifying: a float (or a config value read
    # as one) stringifies to "1e+64", which is not a decimal integer literal and makes
    # `from_amount` raise on the other side of the wire.
    return celaut_pb2.Amount(n=str(int(Decimal(str(amount_mu)))))


def from_amount(amount: celaut_pb2.Amount) -> int:
    return int(amount.n)


def peers_id_iterator(ignore_network: str = None) -> Generator[str, None, None]:
    if ignore_network == "localhost":
        ignore_network = None
    yield from (
        peer_id for peer_id in get_peer_ids()
        if not ignore_network or all(
        not __address_in_network(
            ip_or_uri=uri,
            net=ignore_network
        ) for uri in generate_uris_by_peer_id(
            peer_id=peer_id
        )
    )
    )


def format_uri(ip: str, port: int) -> str:
    """``ip:port`` as a gRPC target, bracketing IPv6 literals.

    A bare ``2001:db8::1:8080`` is not a parseable target -- the host has to be
    ``[2001:db8::1]:8080``. Announcing IPv6 became possible once a node advertises
    every interface (issue #236), so the formatting can no longer assume IPv4.
    """
    try:
        if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address):
            return f"[{ip}]:{port}"
    except ValueError:
        pass  # A DNS name: no brackets.
    return f"{ip}:{port}"


def generate_uris_by_peer_id(peer_id: str) -> typing.Generator[str, None, None]:
    """Every address of ``peer_id`` that this node can open a gRPC channel to.

    Non-TCP addresses are skipped, not merely left unprobed: every consumer of this
    feeds the result straight to ``grpc.insecure_channel``, so yielding a UDP endpoint
    would hand them an address that can never answer -- and, since callers take the
    *first* result, it would shadow the peer's working TCP address indefinitely.
    An empty transport is a row from before addresses declared one; those were all TCP
    gateways.
    """
    yield from (
        format_uri(ip, port) for ip, port, transport in get_peer_directions(
        peer_id=peer_id
    ) if (not transport or transport.strip().lower() == "tcp")
        and is_open(ip=ip, port=port, transport=transport)
    )


_IS_OPEN_CACHE_TTL_SECONDS = 30
_IS_OPEN_CACHE_MAX_ENTRIES = 4096
_is_open_cache: "collections.OrderedDict[typing.Tuple[str, int], typing.Tuple[bool, float]]" = (
    collections.OrderedDict()
)
_is_open_cache_lock = threading.Lock()


def is_open(ip: str, port: int, transport: str = "tcp") -> bool:
    """Whether ``ip:port`` accepts a TCP connection, cached for a short while.

    Each check is a 1s-timeout connect, and generate_uris_by_peer_id (and the ~15
    call sites that take its first result) run it on every call -- accumulating
    several addresses per peer (issue #236) multiplies how often that timeout gets
    paid. A short TTL trades a little staleness for not re-paying it on every call
    within the same handful of seconds.

    ``transport`` is what the peer declared for this address. Only TCP can be probed
    this way: a ``connect()`` on a datagram socket sends nothing and always succeeds
    locally, so it would answer "open" for every UDP address, reachable or not. A
    non-TCP address is therefore reported as usable without probing -- claiming it is
    open on no evidence is no worse than the meaningless probe, and dropping it would
    hide an address that may be perfectly reachable.

    Bounded: the keys come from peer-announced addresses and ``IntroducePeer`` accepts
    an unbounded URI list from anyone, so an unbounded dict would be a memory sink.
    Expired entries are swept and the oldest are evicted past the cap.
    """
    # Empty transport = a legacy row from before addresses declared one; those were
    # all TCP gateways, so probing them as TCP keeps the old behaviour.
    if transport and transport.strip().lower() != "tcp":
        return True

    key = (ip, port)
    now = time.monotonic()

    with _is_open_cache_lock:
        cached = _is_open_cache.get(key)
        if cached is not None and cached[1] > now:
            _is_open_cache.move_to_end(key)
            return cached[0]

    try:
        family = socket.AF_INET
        try:
            if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address):
                family = socket.AF_INET6
        except ValueError:
            pass  # A DNS name: let getaddrinfo pick via AF_INET.
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((ip, port))
        sock.close()
        result = True
    except Exception:
        result = False

    with _is_open_cache_lock:
        for stale_key in [k for k, (_, expiry) in _is_open_cache.items() if expiry <= now]:
            _is_open_cache.pop(stale_key, None)
        _is_open_cache[key] = (result, now + _IS_OPEN_CACHE_TTL_SECONDS)
        _is_open_cache.move_to_end(key)
        while len(_is_open_cache) > _IS_OPEN_CACHE_MAX_ENTRIES:
            _is_open_cache.popitem(last=False)
    return result
