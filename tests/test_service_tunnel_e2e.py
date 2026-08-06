"""End-to-end tunnel tests: real gRPC server, real beeRPC framing, real sockets.

``test_service_tunnel.py`` covers the relay in isolation. This one wires the whole
path together — ``nodo tunnel``'s client bridge → beeRPC → a live
``Gateway.ServiceTunnel`` → the relay → a TCP service — because the parts that
broke historically were the *seams*: the bee_rpc index maps on either side of the
gateway, which no unit test of the relay can exercise.

Only the two instance lookups are mocked.
"""

import socket
import threading
import unittest
from concurrent import futures
from typing import Callable, Tuple
from unittest.mock import patch

IMPORT_ERROR = None
try:
    import grpc
    from bee_rpc.client import client_grpc
    from protos import celaut_pb2 as celaut
    from protos import celaut_pb2_grpc
    from src.tunneling import tunnel_client
    from src.gateway.gateway import Gateway
    from src.tunneling import rpc_tunnel
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    grpc = None  # type: ignore[assignment]

RPC_TIMEOUT_S = 10.0


def _start_echo_service() -> int:
    """A TCP service that echoes until the caller half-closes. Returns its port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    server.settimeout(RPC_TIMEOUT_S)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            while True:
                conn, _ = server.accept()
                threading.Thread(target=_echo_once, args=(conn,), daemon=True).start()
        except OSError:
            pass

    threading.Thread(target=serve, name="e2e-echo-service", daemon=True).start()
    return port


def _echo_once(conn: socket.socket) -> None:
    with conn:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            conn.sendall(data)


def _start_udp_echo_service() -> int:
    """A UDP service that echoes each datagram back to its sender."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(RPC_TIMEOUT_S)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            while True:
                datagram, source = server.recvfrom(65535)
                server.sendto(datagram, source)
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=serve, name="e2e-udp-echo-service", daemon=True).start()
    return port


def _instance_declaring(port: int, transport: str = "tcp") -> bytes:
    return celaut.Instance(
        api=celaut.Service.Api(
            slot=[
                celaut.Service.Api.Slot(
                    port=port,
                    transport=celaut.Service.Api.Protocol(tags=[transport]),
                )
            ]
        ),
        uri_slot=[celaut.Instance.Uri_Slot(internal_port=port)],
    ).SerializeToString()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.service_port = _start_echo_service()

        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        celaut_pb2_grpc.add_GatewayServicer_to_server(Gateway(), self.grpc_server)
        gateway_port = self.grpc_server.add_insecure_port("127.0.0.1:0")
        self.grpc_server.start()
        self.gateway = f"127.0.0.1:{gateway_port}"

        self.channel = grpc.insecure_channel(self.gateway)
        self.stub = celaut_pb2_grpc.GatewayStub(self.channel)

        # Everything except the instance catalogue and gas accounting is real.
        # The instance is mocked rather than inserted, so spend_gas would refuse
        # every charge for an instance it cannot find; metering itself is covered
        # by ServiceTunnelGasTests.
        self.db_patches = [
            patch.object(
                rpc_tunnel.sc,
                "get_internal_instance",
                side_effect=lambda id: _instance_declaring(self.service_port) if id == "tok" else None,
            ),
            patch.object(
                rpc_tunnel.sc,
                "get_internal_ip",
                side_effect=lambda id: "127.0.0.1" if id == "tok" else None,
            ),
            patch("src.manager.manager.spend_gas", return_value=True),
        ]
        for db_patch in self.db_patches:
            db_patch.start()

    def tearDown(self):
        for db_patch in self.db_patches:
            db_patch.stop()
        self.channel.close()
        self.grpc_server.stop(0)

    def _open_tunnel(self, messages) -> list:
        return list(
            client_grpc(
                method=self.stub.ServiceTunnel,
                input=iter(messages),
                indices_parser={0: bytes},
                partitions_message_mode_parser=True,
                indices_serializer={1: celaut.TokenMessage},
                timeout=RPC_TIMEOUT_S,
            )
        )

    def test_payload_crosses_the_whole_path(self):
        """The seam test: TokenMessage at index 1, payload at index 0, both ways."""
        replies = self._open_tunnel(
            [
                celaut.TokenMessage(token="tok", slot=str(self.service_port)),
                b"through-the-tunnel",
            ]
        )

        self.assertEqual(
            b"".join(chunk for chunk in replies if isinstance(chunk, bytes)),
            b"through-the-tunnel",
        )

    def test_unknown_token_fails_with_a_status_not_an_empty_stream(self):
        with self.assertRaises(grpc.RpcError) as caught:
            self._open_tunnel([celaut.TokenMessage(token="ghost", slot="8080")])

        self.assertEqual(caught.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)
        self.assertIn("No local instance", caught.exception.details())

    def test_undeclared_slot_fails_with_a_status(self):
        with self.assertRaises(grpc.RpcError) as caught:
            self._open_tunnel([celaut.TokenMessage(token="tok", slot="22")])

        self.assertEqual(caught.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)
        self.assertIn("not declared", caught.exception.details())

    def test_cli_udp_listener_bridges_datagrams_to_the_service(self):
        """A local UDP client reaches a UDP slot, datagram boundaries intact."""
        udp_service_port = _start_udp_echo_service()

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        listener_port = listener.getsockname()[1]

        with patch.object(
            rpc_tunnel.sc,
            "get_internal_instance",
            return_value=_instance_declaring(udp_service_port, transport="udp"),
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), patch.object(
            rpc_tunnel, "_udp_idle_timeout", return_value=0.5
        ):
            threading.Thread(
                target=tunnel_client.serve_udp,
                kwargs={
                    "listener": listener,
                    "token": "tok",
                    "slot": udp_service_port,
                    "gateway": self.gateway,
                    "idle_timeout": 0.5,
                },
                daemon=True,
            ).start()

            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(RPC_TIMEOUT_S)
            try:
                client.sendto(b"first", ("127.0.0.1", listener_port))
                first, _ = client.recvfrom(65535)
                client.sendto(b"second-and-longer", ("127.0.0.1", listener_port))
                second, _ = client.recvfrom(65535)
            finally:
                client.close()
                listener.close()

        # Two datagrams in, two distinct datagrams out — never coalesced.
        self.assertEqual(first, b"first")
        self.assertEqual(second, b"second-and-longer")

    def test_cli_client_bridges_a_local_socket_to_the_service(self):
        """`nodo tunnel`'s per-connection bridge, end to end."""
        local_end, client_end = socket.socketpair()

        bridge = threading.Thread(
            target=tunnel_client.bridge_tcp_connection,
            kwargs={
                "sock": local_end,
                "label": "test",
                "token": "tok",
                "slot": self.service_port,
                "gateway": self.gateway,
                "log": lambda message: None,
            },
            daemon=True,
        )
        bridge.start()

        try:
            client_end.sendall(b"local-client-speaking")
            client_end.shutdown(socket.SHUT_WR)  # Half-close, as a real client would.

            client_end.settimeout(RPC_TIMEOUT_S)
            received = b""
            while True:
                data = client_end.recv(4096)
                if not data:
                    break
                received += data
        finally:
            client_end.close()
            bridge.join(timeout=RPC_TIMEOUT_S)

        self.assertEqual(received, b"local-client-speaking")


if __name__ == "__main__":
    unittest.main()
