"""`_store_peer_uris` stores an address only if this node speaks what it announces.

Two independent declarations ride on a `Peer.Uri` and both have to match for the
address to be dialable:

* `transport.tags` -- the family the address is reached over (tcp, udp), resolved
  against what the host supports.
* `protocol_stack` -- what is layered on top of it, spelled out in `formal`: the TLS
  extension OID, what the certificate signature covers, the set of gateway RPCs.

An address whose stack is something else reaches a listener and then fails in the
handshake, with nothing in the error naming the disagreement. Refusing it at
registration is what turns that into "we do not speak that", which is the same trade
`speaks_our_signature_scheme` makes for the announcement's signature.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.identity import transport_stack
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    manager = None  # type: ignore[assignment]

PEER = "peer-1"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StorePeerUrisTransportStackTests(unittest.TestCase):
    def setUp(self):
        self.added = []
        patcher = mock.patch.object(
            manager.sc,
            "add_peer_uri",
            side_effect=lambda uri, peer_id, transport: self.added.append(
                (uri.ip, uri.port, transport)
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _peer(self, *, declare=True, ip="10.0.0.1"):
        peer = celaut_pb2.Peer()
        uri = peer.uri.add(ip=ip, port=8080)
        uri.transport.tags.append("tcp")
        if declare:
            transport_stack.declare_transport_stack(uri, prose=False)
        return peer

    def test_an_address_speaking_our_stack_is_stored(self):
        stored = manager._store_peer_uris(self._peer(), PEER)
        self.assertEqual(stored, [("10.0.0.1", 8080)])
        self.assertEqual(self.added, [("10.0.0.1", 8080, "tcp")])

    def test_a_foreign_stack_is_refused_though_its_transport_is_supported(self):
        # tcp resolves fine; what rides on it does not. Storing the address anyway is
        # what the declaration exists to prevent.
        peer = self._peer()
        peer.uri[0].protocol_stack[0].formal = b"host_key_oid=1.2.3.4\n"

        stored = manager._store_peer_uris(peer, PEER)

        self.assertEqual(stored, [])
        self.assertEqual(self.added, [])

    def test_a_refused_address_is_left_out_of_what_the_caller_keeps(self):
        # The return value is what a caller prunes against, so an address refused here
        # must not survive as a stale row carrying its previous transport.
        peer = self._peer()
        peer.uri[0].protocol_stack[0].formal = b"host_key_oid=1.2.3.4\n"
        good = peer.uri.add(ip="10.0.0.2", port=8080)
        good.transport.tags.append("tcp")
        transport_stack.declare_transport_stack(good, prose=False)

        self.assertEqual(manager._store_peer_uris(peer, PEER), [("10.0.0.2", 8080)])

    def test_an_address_declaring_no_stack_is_stored(self):
        # Silence is not a foreign protocol: an announcement predating the declaration
        # carries no components, and what it would have declared is what this node
        # speaks anyway.
        stored = manager._store_peer_uris(self._peer(declare=False), PEER)
        self.assertEqual(stored, [("10.0.0.1", 8080)])

    def test_rewording_the_prose_does_not_refuse_an_address(self):
        # Only `formal` and the tags decide. Prose is the same protocol written out,
        # and no two implementations would word it identically.
        peer = self._peer()
        for component in peer.uri[0].protocol_stack:
            component.prose = "however this peer chose to describe it"

        self.assertEqual(manager._store_peer_uris(peer, PEER), [("10.0.0.1", 8080)])


if __name__ == "__main__":
    unittest.main()
