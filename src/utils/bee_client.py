"""Every gRPC-framing call this node makes through bee_rpc, in one place.

bee_rpc is the wire format every Gateway RPC actually speaks: each one is declared as
``stream buffer.Buffer -> stream buffer.Buffer`` in celaut.proto, and bee_rpc is what
turns that raw byte stream into the typed messages (``Peer``, ``Client``, ...) the
rest of this codebase works with.

Two layers:

- The **named, per-RPC methods** (``introduce_peer``, ``start_service``, ...) are what
  every outbound caller should reach for. Each one already knows that RPC's own wire
  shape -- which stub method, which envelope, which response type -- so a call site
  needs nothing but the channel and the Python values that RPC actually cares about, no
  ``celaut_pb2_grpc``/indices boilerplate repeated at every call site that happens to
  ask a peer the same question.
- The **generic primitives** (``parse``/``parse_one``/``respond``/``call``/``call_one``)
  are what those methods are built from, and what a *server* handler (a gateway.py
  method, an iterable) still uses directly -- an inbound request's own shape is
  intrinsic to the handler answering it, not something reused across call sites the way
  an outbound RPC is.

Not included: ``bee_rpc``'s content-addressed block storage/packing API
(``block_builder``, ``Dir`` as a *packing* concept, ``Enviroment``/``modify_env``,
hashing helpers). That is a different concern -- reading and writing packed service
blobs on disk -- from framing a gRPC call, and the files that do it keep importing
``bee_rpc`` directly.
"""
import concurrent.futures
from typing import Any, Optional, Union

from bee_rpc import client as bee
from bee_rpc import buffer_pb2
from bee_rpc.control import StreamControl

from protos import celaut_pb2, celaut_pb2_grpc
from protos.gateway_bee import (
    GenerateClient_output_indices,
    StartService_input_indices,
    StartService_input_message_mode,
)

# Re-exported so a caller that only talks to BeeClient never needs a second import
# for the couple of bee_rpc names that are types/utilities rather than calls.
Buffer = buffer_pb2.Buffer
Dir = bee.Dir


class BeeClient:
    """Stateless: every method is a thin wrapper around one bee_rpc shape."""

    # ------------------------------------------------------------------
    # Generic primitives. Server handlers use these directly; the named
    # methods below are built out of ``call``/``call_one``.
    # ------------------------------------------------------------------

    @staticmethod
    def parse(
            request_iterator, indices, partitions_message_mode: Any = True,
            control: Optional[StreamControl] = None, timeout: Optional[float] = None,
    ):
        """The raw, possibly-multi-message parse -- iterate this (``for r in ...``)
        when a request's envelope can carry more than one kind of message (a
        pattern-matching loop over the result), or when its response needs
        block-skip (``control``, issue #371).

        ``timeout``, when given, bounds how long any single next message may take to
        arrive -- not the call as a whole -- so a handler whose whole request is a
        short, fixed set of small control messages (a client_id, a hash, a resource
        profile, ...) cannot be held open forever by a peer that opens the stream and
        sends less than it declared, or sends it out of the order this parse happens
        to look for first. Leave it unset for a handler that legitimately keeps
        reading past its own parsing (``control``'s later ``watch()`` phase during a
        large response, ``ServiceTunnel``'s relay) -- there, a quiet stretch is normal,
        not an attack.
        """
        generator = bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=indices,
            partitions_message_mode=partitions_message_mode,
            control=control,
        )
        return BeeClient._bounded(generator, timeout) if timeout else generator

    @staticmethod
    def parse_one(
            request_iterator, indices, partitions_message_mode: Any = True, default=None,
            timeout: Optional[float] = None,
    ):
        """The common case: exactly one message is expected out of the request, or
        ``default`` when the caller sent nothing (or the wrong type)."""
        return next(
            BeeClient.parse(request_iterator, indices, partitions_message_mode, timeout=timeout),
            default,
        )

    _NOTHING = object()

    @staticmethod
    def _bounded(iterator, timeout: float):
        """Wrap ``iterator`` so each ``next()`` gives up after ``timeout`` seconds.

        Every pull is run on a dedicated one-off worker thread, because nothing short
        of that can interrupt a call blocked on socket I/O -- gRPC's Python request
        iterators have no cooperative-cancellation hook to poll instead. Past a
        timeout the worker is simply abandoned (``shutdown(wait=False)``): it is not
        killed, but it is never asked for another item either, and it ends on its own
        the moment the underlying gRPC call is torn down -- which raising here, and
        the caller refusing the RPC in response, is exactly what triggers.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            while True:
                future = executor.submit(next, iterator, BeeClient._NOTHING)
                try:
                    item = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"No message received within {timeout}s."
                    )
                if item is BeeClient._NOTHING:
                    return
                yield item
        finally:
            executor.shutdown(wait=False)

    @staticmethod
    def respond(message_iterator=None, indices=None, control: Optional[StreamControl] = None):
        """Serialize a handler's response -- ``yield from`` this."""
        return bee.serialize_to_buffer(
            message_iterator=message_iterator,
            indices=indices,
            control=control,
        )

    @staticmethod
    def call(
            method,
            input=None,
            indices_parser=None,
            indices_serializer=None,
            partitions_message_mode_parser: Any = True,
            timeout: Optional[float] = None,
            block_skip: bool = False,
    ):
        """The raw response generator -- iterate this for a streamed response
        (``GetService``'s blocks) or hand it straight back to a caller that relays it
        itself (``ServiceTunnel``'s two-way bridge). ``block_skip`` opts into
        bee_rpc's reverse-direction block skipping; see ``client_grpc``'s own
        docstring for what that changes about the request generator's lifetime.
        """
        return bee.client_grpc(
            method=method,
            input=input,
            indices_parser=indices_parser,
            indices_serializer=indices_serializer,
            partitions_message_mode_parser=partitions_message_mode_parser,
            timeout=timeout,
            block_skip=block_skip,
        )

    @staticmethod
    def call_one(
            method,
            input=None,
            indices_parser=None,
            indices_serializer=None,
            partitions_message_mode_parser: Any = True,
            timeout: Optional[float] = None,
            default=None,
    ):
        """The common case: exactly one response message is expected, or
        ``default`` (a closed port, a peer that never answers, ...)."""
        return next(
            BeeClient.call(
                method, input, indices_parser, indices_serializer,
                partitions_message_mode_parser, timeout,
            ),
            default,
        )

    # ------------------------------------------------------------------
    # Named, per-RPC outbound calls -- one per Gateway RPC this node calls on a
    # peer. Every ``client_id``/``peer_id``/``signature`` parameter is optional
    # and, left as "", is simply not attached -- the same "absent, not empty"
    # convention client_gate.require_caller and generate_client_or_pow_required
    # already use.
    # ------------------------------------------------------------------

    @staticmethod
    def get_peer_info(channel, client_id: str = "") -> Optional[celaut_pb2.Peer]:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GetPeerInfo,
            input=celaut_pb2.Client(client_id=client_id) if client_id else None,
            indices_serializer=celaut_pb2.Client,
            indices_parser=celaut_pb2.Peer,
        )

    @staticmethod
    def introduce_peer(
            channel, peer: celaut_pb2.Peer, client_id: str = ""
    ) -> Optional[celaut_pb2.RecursionGuard]:
        if client_id:
            indices_serializer = {1: celaut_pb2.Peer, 2: celaut_pb2.Client}
            input_messages = [peer, celaut_pb2.Client(client_id=client_id)]
        else:
            indices_serializer = celaut_pb2.Peer
            input_messages = peer
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).IntroducePeer,
            input=input_messages,
            indices_serializer=indices_serializer,
            indices_parser=celaut_pb2.RecursionGuard,
        )

    @staticmethod
    def generate_client(
            channel,
            client_id: str = "",
            challenge: str = "",
            pow_solution: str = "",
            peer_id: str = "",
            signature: str = "",
    ) -> Optional[Union[celaut_pb2.Client, celaut_pb2.PoWRequired]]:
        """``client_id`` is the caller's proposed UUID4 (or "" for "mint me one");
        ``challenge``/``pow_solution`` are only set on the retry that answers a
        ``PoWRequired`` (issue #361); ``peer_id``/``signature`` optionally prove the
        caller is also a known peer, over that same ``client_id``, so this node can
        bind the two the moment it creates it (or, once known, via
        ``associate_client``).
        """
        message = celaut_pb2.Client(client_id=client_id)
        if challenge:
            message.challenge = challenge
        if pow_solution:
            message.pow_solution = pow_solution
        if peer_id:
            message.peer_id = peer_id
        if signature:
            message.signature = signature
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GenerateClient,
            input=message,
            indices_parser=dict(GenerateClient_output_indices),
            indices_serializer=celaut_pb2.Client,
        )

    @staticmethod
    def associate_client(
            channel, client_id: str, peer_id: str = "", signature: str = ""
    ) -> Optional[celaut_pb2.AssociateClientOutput]:
        message = celaut_pb2.Client(client_id=client_id)
        if peer_id:
            message.peer_id = peer_id
        if signature:
            message.signature = signature
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).AssociateClient,
            input=message,
            indices_parser=celaut_pb2.AssociateClientOutput,
            indices_serializer=celaut_pb2.Client,
        )

    @staticmethod
    def resolve_network(
            channel, network: celaut_pb2.Service.Network, timeout: Optional[float] = None,
            client_id: str = "",
    ) -> Optional[celaut_pb2.ConfigurationFile.NetworkResolution]:
        if client_id:
            indices_serializer = {1: celaut_pb2.Service.Network, 2: celaut_pb2.Client}
            input_messages = [network, celaut_pb2.Client(client_id=client_id)]
        else:
            indices_serializer = celaut_pb2.Service.Network
            input_messages = network
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).ResolveNetwork,
            input=input_messages,
            indices_serializer=indices_serializer,
            indices_parser=celaut_pb2.ConfigurationFile.NetworkResolution,
            timeout=timeout,
        )

    @staticmethod
    def chat(channel, message: celaut_pb2.ChatMessage) -> Optional[celaut_pb2.ChatAck]:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).Chat,
            input=message,
            indices_parser=celaut_pb2.ChatAck,
        )

    @staticmethod
    def get_metrics(channel, token: str) -> celaut_pb2.Metrics:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GetMetrics,
            input=celaut_pb2.TokenMessage(token=token),
            indices_parser=celaut_pb2.Metrics,
        )

    @staticmethod
    def generate_deposit_token(channel, client_id: str) -> Optional[celaut_pb2.TokenMessage]:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GenerateDepositToken,
            input=celaut_pb2.Client(client_id=client_id),
            indices_parser=celaut_pb2.TokenMessage,
        )

    @staticmethod
    def payable(channel, payment: celaut_pb2.Payment) -> None:
        BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).Payable,
            input=payment,
        )

    @staticmethod
    def get_resource_availability(
            channel,
            resources: celaut_pb2.Service.Container.Resources,
            timeout: Optional[float] = None,
            client_id: str = "",
    ) -> Optional[celaut_pb2.ResourceAvailability]:
        if client_id:
            indices_serializer = {1: celaut_pb2.Service.Container.Resources, 2: celaut_pb2.Client}
            input_messages = [resources, celaut_pb2.Client(client_id=client_id)]
        else:
            indices_serializer = celaut_pb2.Service.Container.Resources
            input_messages = resources
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GetResourceAvailability,
            input=input_messages,
            indices_serializer=indices_serializer,
            indices_parser=celaut_pb2.ResourceAvailability,
            timeout=timeout,
        )

    @staticmethod
    def get_service_estimated_cost(
            channel, message_iterator, timeout: Optional[float] = None
    ) -> Optional[celaut_pb2.EstimatedCost]:
        """``message_iterator`` is a ``StartService_input_indices``-shaped envelope
        (``src.utils.utils.service_extended``'s output) -- the same one used to
        quote and to actually launch, so a quote and the launch it precedes are
        always priced off the same declaration.
        """
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).GetServiceEstimatedCost,
            input=message_iterator,
            indices_parser=celaut_pb2.EstimatedCost,
            indices_serializer=StartService_input_indices,
            timeout=timeout,
        )

    @staticmethod
    def start_service(channel, message_iterator, timeout: Optional[float] = None) -> celaut_pb2.ServiceInstance:
        """No default: a peer that sends nothing back is this call's failure to
        report, not a "no instance" this node can quietly treat as one.
        """
        return next(BeeClient.call(
            method=celaut_pb2_grpc.GatewayStub(channel).StartService,
            input=message_iterator,
            indices_parser=celaut_pb2.ServiceInstance,
            indices_serializer=StartService_input_indices,
            timeout=timeout,
        ))

    @staticmethod
    def stop_service(channel, token: str) -> Optional[celaut_pb2.Refund]:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).StopService,
            input=celaut_pb2.TokenMessage(token=token),
            indices_parser=celaut_pb2.Refund,
        )

    @staticmethod
    def modify_deposit(
            channel, difference: celaut_pb2.Amount, service_token: str
    ) -> Optional[celaut_pb2.ModifyDepositOutput]:
        return BeeClient.call_one(
            method=celaut_pb2_grpc.GatewayStub(channel).ModifyDeposit,
            input=celaut_pb2.ModifyDepositInput(difference=difference, service_token=service_token),
            indices_parser=celaut_pb2.ModifyDepositOutput,
        )

    @staticmethod
    def get_service(channel, hash_message: celaut_pb2.Metadata.HashTag.Hash, client_id: str = ""):
        """Streamed, and the response can be large (a whole packed service), so this
        returns the raw generator -- iterate it -- rather than collapsing it into one
        message the way ``call_one`` does for everything else here.

        ``client_id``, like everywhere else in this file, is "absent, not empty": a
        ``Client(client_id="")`` would not even be a no-op to send -- protobuf
        serializes an all-default message to zero bytes, which bee_rpc's framing
        cannot tell apart from no message at all, so omitting it costs nothing.
        ``GetServiceIterable`` waits for both a Client and a resolving Hash, in
        whichever order they arrive, up to ``client_gate.simple_rpc_timeout_seconds``
        -- a caller that never sends one just spends that whole wait before the
        server falls back to treating it as unauthenticated.
        """
        if client_id:
            indices_serializer = {1: celaut_pb2.Metadata.HashTag.Hash, 2: celaut_pb2.Client}
            input_messages = [celaut_pb2.Client(client_id=client_id), hash_message]
        else:
            indices_serializer = celaut_pb2.Metadata.HashTag.Hash
            input_messages = hash_message
        return BeeClient.call(
            method=celaut_pb2_grpc.GatewayStub(channel).GetService,
            input=input_messages,
            indices_serializer=indices_serializer,
            indices_parser=StartService_input_indices,  # Not all indices are used, but still the same shape.
            partitions_message_mode_parser=StartService_input_message_mode,
            # Tell the peer which blocks of what it sends we already hold, so it
            # stops mid-block instead of us draining and discarding bytes it did
            # not need to send (issue #371). Ignored by a peer that does not
            # honour it, in which case the response arrives in full as before.
            block_skip=True,
        )

    @staticmethod
    def service_tunnel(channel, outbound):
        """A two-way byte pipe, not a request/response -- returns the raw generator
        for the caller to relay itself in both directions.
        """
        return BeeClient.call(
            method=celaut_pb2_grpc.GatewayStub(channel).ServiceTunnel,
            input=outbound,
            indices_parser={0: bytes},
            indices_serializer={1: celaut_pb2.TokenMessage},
        )
