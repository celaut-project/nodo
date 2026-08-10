"""Tests for service tunneling (``src/tunneling/rpc_tunnel.py``).

The relay is exercised against a **real** TCP server on loopback — only the two
database lookups (instance record, internal IP) are mocked, so the handshake,
slot validation, half-close and full-duplex relay all run for real.
"""

import socket
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from typing import Callable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from bee_rpc import client as bee
    from protos import celaut_pb2 as celaut
    from src.tunneling import rpc_tunnel
    from src.utils.config import ConfigManager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    bee = None  # type: ignore[assignment]
    celaut = None  # type: ignore[assignment]
    ConfigManager = None  # type: ignore[assignment]
    rpc_tunnel = None  # type: ignore[assignment]

# Every relay in these tests gets a deadline instead of `lambda: True`, so a
# regression that parks the reader fails the suite in seconds instead of hanging.
RELAY_DEADLINE_S = 5.0


def _deadline(seconds: float = RELAY_DEADLINE_S) -> Callable[[], bool]:
    expires_at = time.monotonic() + seconds
    return lambda: time.monotonic() < expires_at


@contextmanager
def _charges_granted():
    """Let every charge succeed.

    Relaying is metered (see ``rpc_tunnel.TrafficMeter``), and these tests mock the
    instance catalogue rather than populating it, so ``spend_mu`` would refuse
    every charge for an instance it cannot find. Charging itself is covered by
    ``ServiceTunnelMeteringTests``.
    """
    with patch("src.manager.manager.spend_mu", return_value=True) as spend:
        yield spend


def _start_server(handler: Callable[[socket.socket], None]) -> Tuple[int, threading.Thread]:
    """Serve exactly one connection with ``handler``; return its port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(RELAY_DEADLINE_S)
    port = server.getsockname()[1]

    def run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                handler(conn)
        except OSError:
            pass
        finally:
            server.close()

    thread = threading.Thread(target=run, name="test-tunnel-server", daemon=True)
    thread.start()
    return port, thread


def _echo(conn: socket.socket) -> None:
    """Echo bytes back as they arrive, until the caller half-closes."""
    while True:
        data = conn.recv(4096)
        if not data:
            return
        conn.sendall(data)


def _close_without_replying(conn: socket.socket) -> None:
    return


def _greet_then_echo(conn: socket.socket) -> None:
    """Speak first, the way a real server-push protocol does."""
    conn.sendall(b"GREETING ")
    _echo(conn)


def _start_udp_echo(transform: Callable[[bytes], bytes] = lambda d: d) -> int:
    """Echo every datagram back to its sender; return the port it listens on."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    # The timeout doubles as the test's escape hatch: the thread cannot outlive it.
    server.settimeout(RELAY_DEADLINE_S)
    port = server.getsockname()[1]

    def run() -> None:
        try:
            while True:
                datagram, source = server.recvfrom(65535)
                server.sendto(transform(datagram), source)
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=run, name="test-udp-echo", daemon=True).start()
    return port


def _serialized_instance(port: int, transport: str = "tcp") -> bytes:
    """An instance record declaring exactly one slot on ``port``."""
    return celaut.Instance(
        api=celaut.Service.Api(
            slot=[
                celaut.Service.Api.Slot(
                    port=port,
                    transport=celaut.Service.Api.Protocol(tags=[transport]),
                )
            ]
        ),
        uri_slot=[
            celaut.Instance.Uri_Slot(
                internal_port=port,
                uri=[celaut.Instance.Uri(ip="127.0.0.1", port=port)],
            )
        ],
    ).SerializeToString()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelRelayTests(unittest.TestCase):
    """The happy paths: bytes actually cross the tunnel."""

    def _tunnel(
        self,
        messages: List,
        instance: Optional[bytes],
        ip: Optional[str] = "127.0.0.1",
    ):
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=instance
        ), patch.object(rpc_tunnel.sc, "get_internal_ip", return_value=ip), _charges_granted():
            _conn, relay = rpc_tunnel.service_tunnel(iter(messages), is_active=_deadline())
            return relay

    def test_payload_is_relayed_in_both_directions(self):
        port, _ = _start_server(_echo)
        relay = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"ping"],
            instance=_serialized_instance(port),
        )

        # Byte boundaries are not preserved by contract, so compare the joined stream.
        self.assertEqual(b"".join(relay), b"ping")

    def test_multiple_payload_chunks_are_forwarded_in_order(self):
        port, _ = _start_server(_echo)
        relay = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"first-", b"second"],
            instance=_serialized_instance(port),
        )

        self.assertEqual(b"".join(relay), b"first-second")

    def test_service_can_speak_before_the_caller_sends_payload(self):
        """The relay must not be gated on caller traffic (the old loop was)."""
        port, _ = _start_server(_greet_then_echo)
        relay = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"then-mine"],
            instance=_serialized_instance(port),
        )

        self.assertEqual(b"".join(relay), b"GREETING then-mine")

    def test_caller_payload_stream_may_be_empty(self):
        """Handshake only: the service still gets a connection and can reply."""
        port, _ = _start_server(lambda conn: conn.sendall(b"unsolicited"))
        relay = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port))],
            instance=_serialized_instance(port),
        )

        self.assertEqual(b"".join(relay), b"unsolicited")

    def test_non_bytes_payload_is_skipped_not_fatal(self):
        port, _ = _start_server(_echo)
        relay = self._tunnel(
            [
                celaut.TokenMessage(token="tok", slot=str(port)),
                celaut.TokenMessage(token="stray", slot="1"),  # a second handshake
                b"payload",
            ],
            instance=_serialized_instance(port),
        )

        self.assertEqual(b"".join(relay), b"payload")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelUdpTests(unittest.TestCase):
    """Datagram slots. A UDP relay has no EOF, so it ends on an idle timeout."""

    # Short enough to keep the suite fast, long enough not to race the echo.
    IDLE_TIMEOUT_S = 0.5

    def _tunnel(self, messages: List, port: int, transport: str = "udp"):
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=_serialized_instance(port, transport)
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), patch.object(
            rpc_tunnel, "_udp_idle_timeout", return_value=self.IDLE_TIMEOUT_S
        ), _charges_granted():
            _conn, relay = rpc_tunnel.service_tunnel(iter(messages), is_active=_deadline())
            # Drain inside the patch context: the idle timeout is read per relay.
            return list(relay)

    def test_a_udp_slot_is_tunnelled_not_refused(self):
        port = _start_udp_echo()
        replies = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"datagram"], port
        )

        self.assertEqual(b"".join(replies), b"datagram")

    def test_datagram_boundaries_survive_the_relay(self):
        """The property UDP depends on: N datagrams in, N datagrams out, unmerged."""
        port = _start_udp_echo()
        sent = [b"a", b"bb" * 10, b"c" * 1400]

        replies = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port))] + sent, port
        )

        self.assertEqual([len(r) for r in replies], [len(s) for s in sent])
        self.assertEqual(replies, sent)

    def test_relay_ends_on_idle_silence(self):
        """No EOF exists on UDP, so silence after the caller stops must end it."""
        port = _start_udp_echo(transform=lambda d: b"once")
        started = time.monotonic()

        replies = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"ping"], port
        )
        elapsed = time.monotonic() - started

        self.assertEqual(replies, [b"once"])
        # It waited for silence rather than returning instantly or hanging.
        self.assertGreaterEqual(elapsed, self.IDLE_TIMEOUT_S)
        self.assertLess(elapsed, RELAY_DEADLINE_S)

    def test_zero_length_reply_is_dropped_without_stalling(self):
        """beeRPC cannot carry an empty message; the relay must not emit or hang."""
        port = _start_udp_echo(transform=lambda d: b"")

        replies = self._tunnel(
            [celaut.TokenMessage(token="tok", slot=str(port)), b"ping"], port
        )

        self.assertEqual(replies, [])

    def test_oversized_datagram_is_dropped_but_the_tunnel_survives(self):
        port = _start_udp_echo()

        replies = self._tunnel(
            [
                celaut.TokenMessage(token="tok", slot=str(port)),
                b"x" * 70000,  # Larger than any IPv4 datagram: send() fails.
                b"still-here",
            ],
            port,
        )

        self.assertEqual(replies, [b"still-here"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelRejectionTests(unittest.TestCase):
    """Every refusal must raise TunnelError eagerly, before any byte is relayed."""

    def _expect_refusal(
        self,
        messages: List,
        instance: Optional[bytes],
        ip: Optional[str] = "127.0.0.1",
    ) -> str:
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=instance
        ), patch.object(rpc_tunnel.sc, "get_internal_ip", return_value=ip), _charges_granted():
            with self.assertRaises(rpc_tunnel.TunnelError) as caught:
                rpc_tunnel.service_tunnel(iter(messages), is_active=_deadline())
        return str(caught.exception)

    def test_empty_stream(self):
        self.assertIn("TokenMessage must be sent first", self._expect_refusal([], instance=None))

    def test_first_message_must_be_a_token_message(self):
        message = self._expect_refusal([b"straight-to-payload"], instance=None)
        self.assertIn("must be a TokenMessage", message)

    def test_token_message_without_slot(self):
        message = self._expect_refusal(
            [celaut.TokenMessage(token="tok")], instance=_serialized_instance(8080)
        )
        self.assertIn("no slot", message)

    def test_slot_that_is_not_a_port_number(self):
        message = self._expect_refusal(
            [celaut.TokenMessage(token="tok", slot="http")],
            instance=_serialized_instance(8080),
        )
        self.assertIn("not a port number", message)

    def test_unknown_token(self):
        message = self._expect_refusal(
            [celaut.TokenMessage(token="ghost", slot="8080")], instance=None
        )
        self.assertIn("No local instance", message)

    def test_undeclared_slot_is_refused(self):
        """The core of the check: holding a token must not open every VM port."""
        message = self._expect_refusal(
            [celaut.TokenMessage(token="tok", slot="22")],  # ssh, never declared
            instance=_serialized_instance(8080),
        )
        self.assertIn("not declared", message)
        self.assertIn("8080", message)  # tells the caller what IS declared

    def test_instance_without_internal_ip(self):
        message = self._expect_refusal(
            [celaut.TokenMessage(token="tok", slot="8080")],
            instance=_serialized_instance(8080),
            ip=None,
        )
        self.assertIn("no internal IP", message)

    def test_unreachable_service(self):
        # Bind and close, so the port is declared but nothing listens on it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        message = self._expect_refusal(
            [celaut.TokenMessage(token="tok", slot=str(dead_port))],
            instance=_serialized_instance(dead_port),
        )
        self.assertIn("Cannot reach the service", message)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelMeteringTests(unittest.TestCase):
    """Relaying is metered against the tunnelled instance's balance."""

    # 10 MU == 10 nanoERG, expressed the way an operator writes a price.
    OPEN_ERG = "0.00000001"
    OPEN_MU = 10
    # 2 MU per KiB, quoted per GiB the way the price vector is: 2 * 1024 * 1024 MU.
    NET_ERG_PER_GIB = "0.002097152"
    PER_KB_MU = 2
    INTERVAL_KB = 1  # Bill every KiB, so a small test payload crosses a block.

    @classmethod
    def setUpClass(cls):
        # Import the manager before any config is patched: charging imports it
        # lazily, and that import chain reads unrelated config keys of its own.
        import src.manager.manager  # noqa: F401

    def _rates(self, **overrides):
        """Override only the tunnel rates; ConfigManager is a shared singleton, so
        everything else must still reach the real config."""
        rates = {
            "pricing.TUNNEL_OPEN_ERG": self.OPEN_ERG,
            "pricing.NET_ERG_PER_GIB": self.NET_ERG_PER_GIB,
            "costs.TUNNEL_CHARGE_INTERVAL_KB": self.INTERVAL_KB,
        }
        rates.update(overrides)
        real_get = ConfigManager().get

        def get(key, default=None):
            return rates[key] if key in rates else real_get(key, default)

        return get

    @staticmethod
    @contextmanager
    def _patched_config(get):
        """Patch every ConfigManager the metering path may be holding.

        Prices resolve ``ConfigManager()`` per call while ``rpc_tunnel`` captured its
        own reference at import, and a test module elsewhere swaps the singleton at
        import time -- so under a full-suite run those two can be different objects.
        Patching only one leaves the real config answering half the reads.
        """
        managers = {id(rpc_tunnel.env_manager): rpc_tunnel.env_manager}
        managers.setdefault(id(ConfigManager()), ConfigManager())
        with ExitStack() as stack:
            for manager in managers.values():
                stack.enter_context(patch.object(manager, "get", side_effect=get))
            yield

    def _run_tunnel(self, payload: List[bytes], port: int, spend, rates=None):
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=_serialized_instance(port)
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), self._patched_config(
            rates or self._rates()
        ), patch("src.manager.manager.spend_mu", spend):
            _conn, relay = rpc_tunnel.service_tunnel(
                iter([celaut.TokenMessage(token="tok", slot=str(port))] + payload),
                is_active=_deadline(),
            )
            return b"".join(relay)

    @staticmethod
    def _charges(spend) -> List[int]:
        return [call.kwargs["amount_mu"] for call in spend.call_args_list]

    def test_opening_a_tunnel_is_charged_to_the_instance(self):
        port, _ = _start_server(_echo)
        spend = MagicMock(return_value=True)

        self._run_tunnel([b"ping"], port, spend)

        self.assertTrue(spend.call_args_list)
        first = spend.call_args_list[0]
        self.assertEqual(first.kwargs["id"], "tok")  # the instance pays
        self.assertEqual(first.kwargs["amount_mu"], self.OPEN_MU)

    def test_a_tunnel_is_refused_when_the_open_charge_cannot_be_paid(self):
        port, _ = _start_server(_echo)
        spend = MagicMock(return_value=False)

        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=_serialized_instance(port)
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), self._patched_config(self._rates()), patch("src.manager.manager.spend_mu", spend):
            with self.assertRaises(rpc_tunnel.TunnelError) as caught:
                rpc_tunnel.service_tunnel(
                    iter([celaut.TokenMessage(token="tok", slot=str(port))]),
                    is_active=_deadline(),
                )

        self.assertIn("balance", str(caught.exception))

    def test_relayed_traffic_is_billed_per_block_in_both_directions(self):
        port, _ = _start_server(_echo)
        spend = MagicMock(return_value=True)

        # 2 KiB out, echoed back as 2 KiB in: 4 blocks of 1 KiB at 2 MU each.
        self._run_tunnel([b"x" * 2048], port, spend)

        charges = self._charges(spend)
        self.assertEqual(charges[0], self.OPEN_MU)
        traffic_mu = sum(charges[1:])
        self.assertEqual(traffic_mu, 4 * self.PER_KB_MU)

    def test_running_out_of_funds_stops_relaying_further_traffic(self):
        """Billing is per block, so the block in flight still lands; the next never does."""
        port, _ = _start_server(_echo)
        # Let the open charge through, then refuse everything else.
        spend = MagicMock(side_effect=[True] + [False] * 50)

        received = self._run_tunnel([b"y" * 4096, b"z" * 4096], port, spend)

        self.assertGreaterEqual(spend.call_count, 2)  # charged, then refused
        # The first payload was already on the wire when the charge was refused;
        # the second must never have been forwarded.
        self.assertNotIn(b"z", received)
        self.assertLessEqual(len(received), 4096)

    def test_a_zero_rate_means_traffic_is_not_billed(self):
        port, _ = _start_server(_echo)
        spend = MagicMock(return_value=True)

        self._run_tunnel(
            [b"z" * 4096],
            port,
            spend,
            rates=self._rates(**{"pricing.NET_ERG_PER_GIB": "0"}),
        )

        # Only the open charge; no per-byte billing at all.
        self.assertEqual(self._charges(spend), [self.OPEN_MU])

    def test_all_rates_zero_means_no_charge_at_all(self):
        port, _ = _start_server(_echo)
        spend = MagicMock(return_value=True)

        payload = self._run_tunnel(
            [b"free"],
            port,
            spend,
            rates=self._rates(
                **{"pricing.TUNNEL_OPEN_ERG": "0", "pricing.NET_ERG_PER_GIB": "0"}
            ),
        )

        self.assertEqual(payload, b"free")
        spend.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceTunnelSerializationTests(unittest.TestCase):
    """The relay's output has to survive the gateway's bee_rpc serialization."""

    def _gateway_indices(self) -> dict:
        # Must match what Gateway.ServiceTunnel passes. Rebuilt per call because
        # bee_rpc mutates the map it is given.
        return {1: celaut.TokenMessage, 0: bytes}

    def _serialize(self, relay) -> List:
        return list(
            bee.serialize_to_buffer(
                message_iterator=relay,
                indices=self._gateway_indices(),
            )
        )

    def test_relayed_bytes_serialize_under_the_payload_index(self):
        port, _ = _start_server(_echo)
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=_serialized_instance(port)
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), _charges_granted():
            _conn, relay = rpc_tunnel.service_tunnel(
                iter([celaut.TokenMessage(token="tok", slot=str(port)), b"payload"]),
                is_active=_deadline(),
            )

        buffers = self._serialize(relay)

        self.assertTrue(buffers, "the relayed payload produced no buffers")
        self.assertEqual(b"".join(b.chunk for b in buffers), b"payload")
        for buffer in buffers:
            if buffer.HasField("head"):
                # Index 0 is the raw-bytes payload index; 1 is the handshake.
                self.assertEqual(buffer.head.index, 0)

    def test_service_that_replies_nothing_yields_an_empty_stream(self):
        """A silent service must not become a RuntimeError.

        With a single-entry index map, bee_rpc infers the index by calling next()
        on the message iterator unguarded, so an empty relay would escape as
        `RuntimeError: generator raised StopIteration` instead of a clean,
        empty stream. Declaring both indices is what avoids that path.
        """
        port, _ = _start_server(_close_without_replying)
        with patch.object(
            rpc_tunnel.sc, "get_internal_instance", return_value=_serialized_instance(port)
        ), patch.object(
            rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"
        ), _charges_granted():
            _conn, relay = rpc_tunnel.service_tunnel(
                iter([celaut.TokenMessage(token="tok", slot=str(port)), b"ignored"]),
                is_active=_deadline(),
            )

        self.assertEqual(self._serialize(relay), [])


if __name__ == "__main__":
    unittest.main()
