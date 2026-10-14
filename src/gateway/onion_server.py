"""The gateway as an onion service: a second gRPC server that only Tor connects to.

Tor forwards a connection to the onion service to ``127.0.0.1:<port>`` of this server,
so every call arrives from ``127.0.0.1``. The gateway identifies a local instance by
the IP of the caller (``utils.get_only_the_ip_from_context``), so an onion call would
look like a call from this host. That would let a remote peer act as a local instance.

The origin is therefore told by the port, not by the IP: this server is the only one
that wraps the call context, and the wrapper reports the caller as not having an IP.
The gateway then treats the caller as it treats any peer, one that needs a ``client_id``.
"""

import grpc

from src.identity.grpc_transport import server_credentials

ONION_PEER_PREFIX = "onion:"


class OnionContext:
    """The context of a call that came through the onion service.

    Everything is the real context's, except ``peer()``: the address of the caller is
    this host (Tor's), so it is never reported as an IP.
    """

    def __init__(self, context):
        self._context = context

    def peer(self) -> str:
        return ONION_PEER_PREFIX + str(self._context.peer())

    def __getattr__(self, name):
        return getattr(self._context, name)


def _wrap(handler):
    if handler is None:
        return None
    options = dict(
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )

    def wrapped(behavior):
        return lambda request, context: behavior(request, OnionContext(context))

    if handler.request_streaming and handler.response_streaming:
        return grpc.stream_stream_rpc_method_handler(wrapped(handler.stream_stream), **options)
    if handler.request_streaming:
        return grpc.stream_unary_rpc_method_handler(wrapped(handler.stream_unary), **options)
    if handler.response_streaming:
        return grpc.unary_stream_rpc_method_handler(wrapped(handler.unary_stream), **options)
    return grpc.unary_unary_rpc_method_handler(wrapped(handler.unary_unary), **options)


class OnionOriginInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        return _wrap(continuation(handler_call_details))


def bind_onion_server(server: "grpc.Server", port: int) -> int:
    """Listen on loopback only, with the node's certificate. Returns the bound port.

    TLS runs end to end inside the onion circuit, so a peer still checks the identity of
    the node by its certificate, as on the clearnet.
    """
    return server.add_secure_port(f"127.0.0.1:{port}", server_credentials())
