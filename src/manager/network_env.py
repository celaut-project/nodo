"""Environment-variable based peer filtering for Service.Network resolution.

A ``Service.Network`` is a logical communication domain. Its optional
``environment_variable`` field names an environment variable whose value is used
to select *compatible* peer instances during
``ConfigurationFile.NetworkResolution``: only peers that expose the same value as
the requesting instance belong to the same domain.

These helpers are intentionally free of any database / RPC dependency so they
stay pure and unit-testable in isolation.
"""
from typing import Callable, Dict, List, Optional

from protos import celaut_pb2 as celaut

# Given a resolved peer Instance, return the peer's environment values
# (name -> bytes) or ``None`` when the peer's environment is unknown (e.g.
# externally-resolved DNS peers, which carry no celaut environment).
PeerEnvLookup = Callable[[celaut.Instance], Optional[Dict[str, bytes]]]


def peer_env_matches(
    network: celaut.Service.Network,
    requester_env_values: Optional[Dict[str, bytes]],
    peer_env_values: Optional[Dict[str, bytes]],
) -> bool:
    """A peer belongs to the same communication domain iff the network declares
    no environment filter, or both the requester and the peer expose the *same
    value* for the environment variable named by ``network.environment_variable``.

    This is what lets many instances of the same service (e.g. PostgreSQL)
    coexist while only the matching ones are returned as peers.
    """
    name = network.environment_variable
    if not name:
        return True
    if not requester_env_values or not peer_env_values:
        return False
    want = requester_env_values.get(name)
    got = peer_env_values.get(name)
    return want is not None and want == got


def filter_peers_by_environment(
    network: celaut.Service.Network,
    peers: List[celaut.Instance],
    requester_env_values: Optional[Dict[str, bytes]],
    peer_env_lookup: Optional[PeerEnvLookup],
) -> List[celaut.Instance]:
    """Filter resolved peer Instances by ``network.environment_variable``.

    When the network declares no environment filter, or no per-peer environment
    lookup is available (e.g. externally resolved DNS peers carry no celaut
    environment), peers pass through unchanged so existing behaviour is kept.
    """
    if not network.environment_variable or peer_env_lookup is None:
        return list(peers)
    return [
        peer for peer in peers
        if peer_env_matches(network, requester_env_values, peer_env_lookup(peer))
    ]
