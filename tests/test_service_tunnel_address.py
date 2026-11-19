"""A tunnel by `Instance.Uri`, with no token, and the wake-up of a hibernated instance (#669)."""

import socket
import threading
import time
import unittest
from typing import List, Optional
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.tunneling import rpc_tunnel
    from src.utils import policies
    from tests.test_service_tunnel import _charges_granted, _deadline, _echo, _start_server
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

IP = "127.0.0.1"


def _instance(port: int, **settings) -> bytes:
    """An instance record with one slot on ``port`` that has the given slot policies."""
    slot = celaut.Service.Api.Slot(port=port, transport=celaut.Service.Api.Protocol(tags=["tcp"]))
    for name, value in settings.items():
        policies.set_policy(slot.policies, policies.make(name, value.encode()))
    return celaut.Instance(
        api=celaut.Service.Api(slot=[slot]),
        uri_slot=[celaut.Instance.Uri_Slot(
            internal_port=port, uri=[celaut.Instance.Uri(ip=IP, port=port + 1000)])],
    ).SerializeToString()


_ADDRESS = {"tunnel_auth": "address"}
_WAKING = {"tunnel_auth": "address", "wake": "on_tunnel"}


class _World:
    """The node's view of one instance: its record, its index row and if it sleeps."""

    def __init__(self, port: int, hibernated: bool = False, found: bool = True, **settings):
        self.port = port
        self.hibernated = [hibernated]
        self.wakes: List[str] = []
        self.record = _instance(port, **settings)
        self.found = found

    def patches(self, wake=None, timeout: Optional[float] = None):
        def is_hibernated(id):
            return self.hibernated[0]

        def default_wake(token):
            self.wakes.append(token)
            self.hibernated[0] = False
            return True

        patchers = [
            patch.object(rpc_tunnel.sc, "find_instance_by_uri",
                         return_value=("inst", self.port) if self.found else None),
            patch.object(rpc_tunnel.sc, "get_internal_instance", return_value=self.record),
            patch.object(rpc_tunnel.sc, "get_internal_ip", return_value=IP),
            patch.object(rpc_tunnel.sc, "is_local_instance_hibernated", side_effect=is_hibernated),
            patch.object(rpc_tunnel, "_wake_saved", side_effect=wake or default_wake),
        ]
        if timeout is not None:
            patchers.append(patch.object(rpc_tunnel, "_wake_timeout", return_value=timeout))
        return patchers


def _open(world: _World, payload: bytes = b"ping", **kwargs):
    patchers = world.patches(**kwargs) + [_charges_granted()]
    for p in patchers:
        p.__enter__()
    try:
        uri = celaut.Instance.Uri(ip=IP, port=world.port + 1000)
        conn, relay = rpc_tunnel.service_tunnel(iter([uri, payload]), is_active=_deadline())
        return b"".join(relay)
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HandshakeTests(unittest.TestCase):
    def test_a_uri_is_a_handshake_with_no_token(self):
        handshake = rpc_tunnel._handshake(iter([celaut.Instance.Uri(ip="203.0.113.5", port=18333)]))
        self.assertEqual(handshake.uri, ("203.0.113.5", 18333))
        self.assertEqual(handshake.token, "")
        self.assertFalse(handshake.receipts)

    def test_a_uri_without_an_address_is_refused(self):
        for uri in (celaut.Instance.Uri(), celaut.Instance.Uri(ip=IP), celaut.Instance.Uri(port=1)):
            with self.assertRaises(rpc_tunnel.TunnelError):
                rpc_tunnel._handshake(iter([uri]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AddressTunnelTests(unittest.TestCase):
    def test_an_awake_instance_is_reached_with_the_uri(self):
        port, _ = _start_server(_echo)
        world = _World(port, **_ADDRESS)
        self.assertEqual(_open(world), b"ping")
        self.assertEqual(world.wakes, [])

    def test_an_unknown_uri_is_unavailable(self):
        world = _World(1, found=False, **_ADDRESS)
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(world)

    def test_a_slot_that_does_not_declare_address_is_unavailable(self):
        for settings in ({}, {"tunnel_auth": "token"}, {"wake": "on_tunnel"}):
            with self.assertRaises(rpc_tunnel.TunnelUnavailable, msg=str(settings)):
                _open(_World(1, **settings))

    def test_a_refusal_has_the_same_text_as_an_unknown_uri(self):
        messages = set()
        for world in (_World(1, found=False, **_ADDRESS), _World(1)):
            with self.assertRaises(rpc_tunnel.TunnelUnavailable) as caught:
                _open(world)
            messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1)

    def test_a_slot_that_a_node_refuses_is_unavailable(self):
        world = _World(1, access="direct", tunnel_auth="address")
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(world)

    def test_a_sleeping_instance_with_wake_never_is_unavailable_and_not_woken(self):
        world = _World(1, hibernated=True, **_ADDRESS)
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(world)
        self.assertEqual(world.wakes, [])

    def test_no_failure_gives_a_status_other_than_unavailable(self):
        # An instance with no balance gives RESOURCE_EXHAUSTED on a token tunnel. Here it
        # must not tell the caller that the instance exists.
        port, _ = _start_server(_echo)
        world = _World(port, **_ADDRESS)
        with patch.object(rpc_tunnel.TrafficMeter, "charge_open", return_value=False):
            with self.assertRaises(rpc_tunnel.TunnelUnavailable) as caught:
                _open(world)
        self.assertNotIsInstance(caught.exception, rpc_tunnel.TunnelExhausted)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WakeTests(unittest.TestCase):
    def test_a_sleeping_instance_is_woken_and_reached(self):
        port, _ = _start_server(_echo)
        world = _World(port, hibernated=True, **_WAKING)
        self.assertEqual(_open(world), b"ping")
        self.assertEqual(world.wakes, ["inst"])

    def test_two_calls_at_once_share_one_wake(self):
        server_ports = []
        results = []
        errors = []
        world = _World(0, hibernated=True, **_WAKING)

        def slow_wake(token):
            world.wakes.append(token)
            time.sleep(0.3)
            world.hibernated[0] = False
            return True

        def serve_two():
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((IP, 0))
            server.listen(2)
            server_ports.append(server.getsockname()[1])

            def handle(conn):
                with conn:
                    _echo(conn)
            for _ in range(2):
                conn, _addr = server.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
        threading.Thread(target=serve_two, daemon=True).start()
        while not server_ports:
            time.sleep(0.01)
        world.port = server_ports[0]
        world.record = _instance(world.port, **_WAKING)

        # The patches of the two calls must not be entered and left one inside the other:
        # enter them once and run both calls under them.
        patchers = world.patches(wake=slow_wake) + [_charges_granted()]
        for p in patchers:
            p.__enter__()
        try:
            def run():
                try:
                    uri = celaut.Instance.Uri(ip=IP, port=world.port + 1000)
                    _conn, relay = rpc_tunnel.service_tunnel(
                        iter([uri, b"ping"]), is_active=_deadline())
                    results.append(b"".join(relay))
                except Exception as e:  # pragma: no cover
                    errors.append(e)
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        finally:
            for p in reversed(patchers):
                p.__exit__(None, None, None)
        self.assertEqual(errors, [])
        self.assertEqual(results, [b"ping", b"ping"])
        self.assertEqual(world.wakes, ["inst"])

    def test_a_wake_that_fails_is_unavailable_and_the_instance_stays_asleep(self):
        world = _World(1, hibernated=True, **_WAKING)
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(world, wake=lambda token: False)
        self.assertTrue(world.hibernated[0])

    def test_a_wake_that_raises_is_unavailable(self):
        def boom(token):
            raise RuntimeError("no room")
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(_World(1, hibernated=True, **_WAKING), wake=boom)

    def test_a_wake_that_takes_too_long_is_unavailable_and_goes_on_in_the_background(self):
        world = _World(1, hibernated=True, **_WAKING)
        done = threading.Event()

        def slow(token):
            time.sleep(0.6)
            world.hibernated[0] = False
            done.set()
            return True

        started = time.monotonic()
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            _open(world, wake=slow, timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.55)
        self.assertTrue(done.wait(3))

    def test_the_tunnel_waits_for_the_service_to_listen_after_the_wake(self):
        probe = socket.socket()
        probe.bind((IP, 0))
        port = probe.getsockname()[1]
        probe.close()
        world = _World(port, hibernated=True, **_WAKING)

        def wake_then_listen_later(token):
            world.wakes.append(token)
            world.hibernated[0] = False

            def listen():
                time.sleep(0.5)
                server = socket.socket()
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((IP, port))
                server.listen(1)
                conn, _ = server.accept()
                with conn:
                    _echo(conn)
                server.close()
            threading.Thread(target=listen, daemon=True).start()
            return True

        self.assertEqual(_open(world, wake=wake_then_listen_later, timeout=5), b"ping")

    def test_a_row_that_woke_between_the_check_and_the_wake_is_reached(self):
        port, _ = _start_server(_echo)
        world = _World(port, hibernated=True, **_WAKING)

        def somebody_else_woke_it(token):
            world.hibernated[0] = False
            return False

        self.assertEqual(_open(world, wake=somebody_else_woke_it), b"ping")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AccessDirectTests(unittest.TestCase):
    def test_a_slot_with_access_direct_is_not_reachable_by_token(self):
        record = _instance(1, access="direct")
        with patch.object(rpc_tunnel.sc, "get_internal_instance", return_value=record), \
                patch.object(rpc_tunnel.sc, "get_internal_ip", return_value=IP), \
                _charges_granted():
            with self.assertRaises(rpc_tunnel.TunnelRefused):
                rpc_tunnel.service_tunnel(
                    iter([celaut.TokenMessage(token="tok", slot="1")]), is_active=_deadline())

    def test_a_slot_with_access_tunnel_is_reachable_by_token(self):
        port, _ = _start_server(_echo)
        record = _instance(port, access="tunnel")
        with patch.object(rpc_tunnel.sc, "get_internal_instance", return_value=record), \
                patch.object(rpc_tunnel.sc, "get_internal_ip", return_value=IP), \
                _charges_granted():
            _conn, relay = rpc_tunnel.service_tunnel(
                iter([celaut.TokenMessage(token="tok", slot=str(port)), b"x"]),
                is_active=_deadline())
            self.assertEqual(b"".join(relay), b"x")


class WireTests(unittest.TestCase):
    @unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
    def test_service_tunnel_accepts_a_uri_at_index_3(self):
        from protos.gateway_bee import rpc_input
        self.assertIs(rpc_input("ServiceTunnel")[3], celaut.Instance.Uri)


if __name__ == "__main__":
    unittest.main()
