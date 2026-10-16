"""The DNS client a hidden node resolves names with: one A query to Tor's DNSPort."""
import socket
import struct
import threading
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.routing import registry, tor_dns
    from src.routing.base import RoutingUnavailable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class FakeDnsServer:
    """Answers each query with ``answer(query) -> bytes``, on 127.0.0.1 over UDP."""

    def __init__(self, answer):
        self.answer = answer
        self.queries = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                query, peer = self.sock.recvfrom(4096)
            except OSError:
                return
            self.queries.append(query)
            reply = self.answer(query)
            if reply is not None:
                self.sock.sendto(reply, peer)

    def close(self):
        self.sock.close()


def reply(query, addresses, rcode=0, wrong_id=False):
    """A reply to ``query`` with one A record per address, the name compressed."""
    query_id = struct.unpack("!H", query[:2])[0] ^ (1 if wrong_id else 0)
    question = query[12:]
    header = struct.pack("!HHHHHH", query_id, 0x8180 | rcode, 1, len(addresses), 0, 0)
    answers = b"".join(
        struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(address)
        for address in addresses
    )
    return header + question + answers


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QueryTests(unittest.TestCase):
    def serve(self, answer):
        server = FakeDnsServer(answer)
        self.addCleanup(server.close)
        return server

    def test_the_a_records_of_the_answer(self):
        server = self.serve(lambda q: reply(q, ["93.184.216.34", "93.184.216.35"]))
        self.assertEqual(tor_dns.query_a("example.com", server.address, timeout=2),
                         ["93.184.216.34", "93.184.216.35"])
        [query] = server.queries
        self.assertIn(b"\x07example\x03com\x00", query)
        self.assertEqual(query[-4:], struct.pack("!HH", 1, 1))  # A, IN

    def test_a_name_that_does_not_exist_is_an_error(self):
        server = self.serve(lambda q: reply(q, [], rcode=3))
        with self.assertRaises(tor_dns.NameNotFound):
            tor_dns.query_a("nothing.example", server.address, timeout=2)

    def test_a_transient_failure_is_not_a_name_that_does_not_exist(self):
        server = self.serve(lambda q: reply(q, [], rcode=2))
        with self.assertRaises(ValueError) as caught:
            tor_dns.query_a("example.com", server.address, timeout=2)
        self.assertNotIsInstance(caught.exception, tor_dns.NameNotFound)

    def test_an_answer_without_an_address_is_an_error(self):
        server = self.serve(lambda q: reply(q, []))
        with self.assertRaises(ValueError):
            tor_dns.query_a("example.com", server.address, timeout=2)

    def test_a_reply_to_another_query_is_ignored(self):
        server = self.serve(lambda q: reply(q, ["6.6.6.6"], wrong_id=True))
        with self.assertRaises(ValueError):
            tor_dns.query_a("example.com", server.address, timeout=0.5)

    def test_no_answer_is_a_timeout_error(self):
        server = self.serve(lambda q: None)
        with self.assertRaises(ValueError):
            tor_dns.query_a("example.com", server.address, timeout=0.3)

    def test_an_invalid_name_is_never_sent(self):
        server = self.serve(lambda q: reply(q, ["6.6.6.6"]))
        for bad in ("", "a" * 64 + ".example", "a..b"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    tor_dns.query_a(bad, server.address, timeout=0.3)
        self.assertEqual(server.queries, [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveTests(unittest.TestCase):
    def test_asks_the_dnsport_of_tor(self):
        server = FakeDnsServer(lambda q: reply(q, ["93.184.216.34"]))
        self.addCleanup(server.close)
        with mock.patch.object(registry.TOR, "dns_port", return_value=server.address[1]):
            self.assertEqual(tor_dns.resolve_ipv4("example.com"), ["93.184.216.34"])

    def test_an_address_literal_needs_no_query(self):
        with mock.patch.object(registry.TOR, "dns_port", return_value=None):
            self.assertEqual(tor_dns.resolve_ipv4("93.184.216.34"), ["93.184.216.34"])
            self.assertEqual(tor_dns.resolve_ipv4("[2001:db8::1]"), [])

    def test_nothing_is_resolved_while_tor_is_not_ready(self):
        with mock.patch.object(registry.TOR, "dns_port", return_value=None), \
                mock.patch.object(socket, "getaddrinfo") as host:
            with self.assertRaises(RoutingUnavailable):
                tor_dns.resolve_ipv4("example.com")
        host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
