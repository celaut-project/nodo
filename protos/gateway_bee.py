from typing import Dict, Final, NamedTuple

from bee_rpc import buffer_pb2

from protos import celaut_pb2, pack_pb2

StartService_input_indices = {
    1: celaut_pb2.Client,
    2: celaut_pb2.RecursionGuard,
    3: celaut_pb2.Configuration,
    4: celaut_pb2.Metadata.HashTag.Hash,
    5: celaut_pb2.Metadata,
    6: celaut_pb2.Service,
}
StartService_input_message_mode = {1: True, 2: True, 3: True, 4: True, 5: True, 6: False}  # False yield a Dir.

# GenerateClient answers with one of two messages (issue #361), so both ends need the
# same index for each: bee-rpc numbers a lone message 1 by itself, which would put a
# Client and a PoWRequired on the same index and leave the caller unable to tell which
# it received.
GenerateClient_output_indices = {
    1: celaut_pb2.Client,
    2: celaut_pb2.PoWRequired,
}

PackOutput_indices = {
    1: pack_pb2.PackOutputServiceId,
    2: celaut_pb2.Metadata,
    3: pack_pb2.Service,
    4: pack_pb2.PackOutputError
}


# ---------------------------------------------------------------------------------
# What each Gateway RPC carries, in one place.
#
# Every Gateway RPC has the same gRPC signature (a stream of buffer.Buffer each way),
# so what a method actually takes and returns is not in celaut.proto at all: it is
# which message type bee-rpc carries at which head index, in each direction. Both ends
# have to agree on it byte for byte, which makes it protocol -- and it is declared as
# such in what every address announces (src/identity/transport_stack.py, the
# celaut-gateway layer), built from the table below. The server (src/gateway) and the
# client (src/utils/bee_client.py) read it from here too, so the three cannot drift.
#
# ``auth`` is how the method identifies its caller:
#   client             a Client at index 2 next to the payload at 1 (client_gate)
#   client-in-message  a client_id inside the payload itself (Chat)
#   token              a bearer token inside the payload (an instance token)
#   local-address      the caller's address must be a local instance of the node
#   none               nobody is identified (GenerateClient, which mints the Client)
# ---------------------------------------------------------------------------------

class GatewayRpc(NamedTuple):
    input: Dict[int, type]
    output: Dict[int, type]
    auth: str


AUTH_KINDS: Final = ("client", "client-in-message", "token", "local-address", "none")

GATEWAY_RPCS: Final[Dict[str, GatewayRpc]] = {
    "StartService": GatewayRpc(
        input=StartService_input_indices,
        output={1: celaut_pb2.ServiceInstance},
        auth="client",
    ),
    "StopService": GatewayRpc(
        input={1: celaut_pb2.TokenMessage},
        output={1: celaut_pb2.Refund},
        auth="token",
    ),
    "ModifyDeposit": GatewayRpc(
        input={1: celaut_pb2.ModifyDepositInput},
        output={1: celaut_pb2.ModifyDepositOutput},
        auth="token",
    ),
    "GetPeerInfo": GatewayRpc(
        input={1: celaut_pb2.Client},
        output={1: celaut_pb2.Peer},
        auth="client",
    ),
    "ResolveNetwork": GatewayRpc(
        input={1: celaut_pb2.Service.Network, 2: celaut_pb2.Client},
        output={1: celaut_pb2.ConfigurationFile.NetworkResolution},
        auth="client",
    ),
    "IntroducePeer": GatewayRpc(
        input={1: celaut_pb2.Peer, 2: celaut_pb2.Client},
        output={1: celaut_pb2.RecursionGuard},
        auth="client",
    ),
    "GenerateClient": GatewayRpc(
        input={1: celaut_pb2.Client},
        output=GenerateClient_output_indices,
        auth="none",
    ),
    "AssociateClient": GatewayRpc(
        input={1: celaut_pb2.Client},
        output={1: celaut_pb2.AssociateClientOutput},
        auth="client",
    ),
    "GenerateDepositToken": GatewayRpc(
        input={1: celaut_pb2.Client},
        output={1: celaut_pb2.TokenMessage},
        auth="client",
    ),
    "Payable": GatewayRpc(
        input={1: celaut_pb2.Payment, 2: celaut_pb2.Client},
        output={1: buffer_pb2.Empty},
        auth="client",
    ),
    "ModifyServiceSystemResources": GatewayRpc(
        input={1: celaut_pb2.ModifyServiceSystemResourcesInput},
        output={1: celaut_pb2.ModifyServiceSystemResourcesOutput},
        auth="local-address",
    ),
    "GetServiceEstimatedCost": GatewayRpc(
        input=StartService_input_indices,
        output={1: celaut_pb2.EstimatedCost},
        auth="client",
    ),
    "GetResourceAvailability": GatewayRpc(
        input={1: celaut_pb2.ArchitectureResources, 2: celaut_pb2.Client},
        output={1: celaut_pb2.ResourceAvailability},
        auth="client",
    ),
    "GetService": GatewayRpc(
        input={1: celaut_pb2.Metadata.HashTag.Hash, 2: celaut_pb2.Client},
        # The StartService envelope's own indices, so what GetService returns can be
        # handed to StartService unchanged.
        output={
            4: celaut_pb2.Metadata.HashTag.Hash,
            5: celaut_pb2.Metadata,
            6: celaut_pb2.Service,
        },
        auth="client",
    ),
    "GetMetrics": GatewayRpc(
        input={1: celaut_pb2.TokenMessage},
        output={1: celaut_pb2.Metrics},
        auth="token",
    ),
    "ServiceTunnel": GatewayRpc(
        input={1: celaut_pb2.TokenMessage, 0: bytes},
        output={0: bytes},
        auth="token",
    ),
    "Observe": GatewayRpc(
        input={1: celaut_pb2.ObserveRequest},
        output={1: celaut_pb2.ObserveEvent},
        auth="token",
    ),
    "Chat": GatewayRpc(
        input={1: celaut_pb2.ChatMessage},
        output={1: celaut_pb2.ChatAck},
        auth="client-in-message",
    ),
}


def rpc_input(method: str) -> Dict[int, type]:
    """A fresh copy of ``method``'s request indices.

    A copy because bee_rpc writes into the dict it is handed (it adds ``0: bytes``),
    so passing the table itself would change it for every later call.
    """
    return dict(GATEWAY_RPCS[method].input)


def rpc_output(method: str) -> Dict[int, type]:
    """A fresh copy of ``method``'s response indices. See :func:`rpc_input`."""
    return dict(GATEWAY_RPCS[method].output)
