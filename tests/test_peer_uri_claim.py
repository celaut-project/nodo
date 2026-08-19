"""An address reaches one node, so at most one peer may hold it.

`nodo connect <ip:port>` dials the address itself and gets back a signed `Peer` it
verifies, so whoever answers there is established fact rather than a claim. When a
*different* peer already held that address, its row is stale — the usual cause being
the same host regenerating its wallet mnemonic, which changes the identity key its
peer_id *is*. Left alone, `generate_uris_by_peer_id` yields in insertion order, so the
dead peer_id is the one every `next(...)` picks first.

`claim_uri` is what moves the address across. These tests pin it down against the real
schema built from `migrate.create_tables`.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]

OLD_PEER = "peer-old"
NEW_PEER = "peer-new"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerUriClaimTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate.create_tables(conn.cursor())
        conn.commit()
        self.sc = SQLConnection()
        self._orig = SQLConnection._connection
        SQLConnection._connection = conn
        self.conn = conn
        for peer_id in (OLD_PEER, NEW_PEER):
            conn.execute(
                "INSERT INTO peer (id, advertisement, remote_client_id, balance_mu) "
                "VALUES (?, ?, '', '0')",
                (peer_id, b""),
            )
        conn.commit()

    def tearDown(self):
        SQLConnection._connection = self._orig

    def _add(self, peer_id, ip="10.0.0.1", port=8080):
        uri = celaut_pb2.Peer.Uri(ip=ip, port=port)
        uri.transport.tags.append("tcp")
        self.sc.add_peer_uri(uri=uri, peer_id=peer_id, transport="tcp")

    def _owners(self, ip="10.0.0.1", port=8080):
        return sorted(
            row[0] for row in self.conn.execute(
                "SELECT peer_id FROM uri WHERE ip=? AND port=?", (ip, port)
            ).fetchall()
        )

    def test_the_address_leaves_the_previous_peer(self):
        self._add(OLD_PEER)
        self._add(NEW_PEER)
        self.assertEqual(self._owners(), [NEW_PEER, OLD_PEER])

        self.assertEqual(self.sc.claim_uri(uri="10.0.0.1:8080", peer_id=NEW_PEER), [OLD_PEER])
        self.assertEqual(self._owners(), [NEW_PEER])

    def test_the_claiming_peer_keeps_its_other_addresses(self):
        # Only the claimed address moves: a peer's remaining addresses are untouched,
        # on both sides of the transfer.
        self._add(OLD_PEER, ip="10.0.0.1")
        self._add(OLD_PEER, ip="10.0.0.9")
        self._add(NEW_PEER, ip="10.0.0.1")
        self._add(NEW_PEER, ip="10.0.0.7")

        self.sc.claim_uri(uri="10.0.0.1:8080", peer_id=NEW_PEER)

        self.assertEqual(self._owners(ip="10.0.0.1"), [NEW_PEER])
        self.assertEqual(self._owners(ip="10.0.0.9"), [OLD_PEER])
        self.assertEqual(self._owners(ip="10.0.0.7"), [NEW_PEER])

    def test_a_different_port_on_the_same_host_is_a_different_address(self):
        # Two nodes behind one NAT share an IP and are told apart by port; claiming
        # one must not evict the other.
        self._add(OLD_PEER, port=9090)
        self._add(NEW_PEER, port=8080)

        self.assertEqual(self.sc.claim_uri(uri="10.0.0.1:8080", peer_id=NEW_PEER), [])
        self.assertEqual(self._owners(port=9090), [OLD_PEER])
        self.assertEqual(self._owners(port=8080), [NEW_PEER])

    def test_reconnecting_to_the_same_peer_changes_nothing(self):
        # The common case: `nodo connect` against a peer we already know. It holds the
        # address itself, so there is nobody to take it from.
        self._add(NEW_PEER)
        self.assertEqual(self.sc.claim_uri(uri="10.0.0.1:8080", peer_id=NEW_PEER), [])
        self.assertEqual(self._owners(), [NEW_PEER])

    def test_a_malformed_address_is_refused_rather_than_guessed(self):
        # No port to compare against, so nothing may be deleted: a bad argument must
        # not turn into a wildcard DELETE.
        self._add(OLD_PEER)
        self.assertEqual(self.sc.claim_uri(uri="10.0.0.1", peer_id=NEW_PEER), [])
        self.assertEqual(self._owners(), [OLD_PEER])

    def test_it_accepts_a_uri_message_as_well_as_a_string(self):
        self._add(OLD_PEER)
        uri = celaut_pb2.Instance.Uri(ip="10.0.0.1", port=8080)
        self.assertEqual(self.sc.claim_uri(uri=uri, peer_id=NEW_PEER), [OLD_PEER])
        self.assertEqual(self._owners(), [])


if __name__ == "__main__":
    unittest.main()
