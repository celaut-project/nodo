"""Re-registering a known peer must not duplicate its uri rows, and must accumulate
distinct addresses instead of losing every one but the last advertised.

`update_peer_instance` runs on every re-handshake of a known peer (a reconnect, a
pay-time refresh, a re-introduction). `add_peer_uri` upserts on (peer_id, ip, port),
so it is idempotent by construction and no longer needs a clear-then-reinsert step
(issue #236). A peer's addresses hang off the peer directly: a `Peer.Uri` carries its
own transport, so the `slot` table that used to group them by port is gone. These
tests pin that down against the real schema built from `migrate.create_tables`.
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

PEER = "peer-1"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerUriRefreshTests(unittest.TestCase):
    def setUp(self):
        # Real schema, in-memory: swap the singleton's connection so add_peer_uri runs
        # its actual SQL, not a mock.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate.create_tables(conn.cursor())
        conn.commit()
        self.sc = SQLConnection()
        self._orig = SQLConnection._connection
        SQLConnection._connection = conn
        self.conn = conn
        conn.execute("INSERT INTO peer (id, advertisement, remote_client_id, balance_mu) "
                     "VALUES (?, ?, '', '0')", (PEER, b""))
        conn.commit()

    def tearDown(self):
        SQLConnection._connection = self._orig

    def _uri(self, port=8080, ip="10.0.0.1", expiry=0, transport="tcp"):
        uri = celaut_pb2.Peer.Uri(ip=ip, port=port, expiry_unix_timestamp=expiry)
        uri.transport.tags.append(transport)
        return uri

    def _count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM uri WHERE peer_id=?", (PEER,)
        ).fetchone()[0]

    def test_readding_the_same_address_is_idempotent(self):
        # Re-registering the same address on every re-handshake must not pile up rows.
        for _ in range(3):
            self.sc.add_peer_uri(uri=self._uri(), peer_id=PEER, transport="tcp")
        self.assertEqual(self._count(), 1)

    def test_readding_with_a_new_address_accumulates(self):
        # A peer re-registering from a second address keeps the first one too,
        # instead of losing it the way clear-then-reinsert used to.
        self.sc.add_peer_uri(uri=self._uri(ip="10.0.0.1"), peer_id=PEER, transport="tcp")
        self.sc.add_peer_uri(uri=self._uri(ip="10.0.0.2"), peer_id=PEER, transport="tcp")
        self.assertEqual(self._count(), 2)

    def test_a_second_port_is_its_own_address(self):
        self.sc.add_peer_uri(uri=self._uri(port=8080), peer_id=PEER, transport="tcp")
        self.sc.add_peer_uri(uri=self._uri(port=9090), peer_id=PEER, transport="tcp")
        self.assertEqual(self._count(), 2)

    def test_readvertising_refreshes_the_expiry_in_place(self):
        self.sc.add_peer_uri(uri=self._uri(expiry=1000), peer_id=PEER, transport="tcp")
        self.sc.add_peer_uri(uri=self._uri(expiry=5000), peer_id=PEER, transport="tcp")
        self.assertEqual(self._count(), 1)
        self.assertEqual(self.sc.get_peer_expiry_unix_timestamp(peer_id=PEER), 5000)

    def test_the_declared_transport_is_stored_per_address(self):
        # is_open needs it to know a UDP address cannot be probed with a TCP connect.
        self.sc.add_peer_uri(uri=self._uri(ip="10.0.0.1"), peer_id=PEER, transport="tcp")
        self.sc.add_peer_uri(
            uri=self._uri(ip="10.0.0.2", transport="udp"), peer_id=PEER, transport="udp"
        )
        stored = dict(self.conn.execute(
            "SELECT ip, transport FROM uri WHERE peer_id=?", (PEER,)
        ).fetchall())
        self.assertEqual(stored, {"10.0.0.1": "tcp", "10.0.0.2": "udp"})

    def test_the_soonest_expiry_is_the_one_reported(self):
        # maintain.peer_deposits refreshes a peer once any of its addresses expires.
        self.sc.add_peer_uri(uri=self._uri(ip="10.0.0.1", expiry=9000), peer_id=PEER, transport="tcp")
        self.sc.add_peer_uri(uri=self._uri(ip="10.0.0.2", expiry=3000), peer_id=PEER, transport="tcp")
        self.assertEqual(self.sc.get_peer_expiry_unix_timestamp(peer_id=PEER), 3000)

    def test_no_expiry_reads_back_as_none(self):
        self.sc.add_peer_uri(uri=self._uri(expiry=0), peer_id=PEER, transport="tcp")
        self.assertIsNone(self.sc.get_peer_expiry_unix_timestamp(peer_id=PEER))


if __name__ == "__main__":
    unittest.main()
