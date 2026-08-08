"""Re-registering a known peer must not duplicate its slot/uri rows, and must
accumulate distinct addresses instead of losing every one but the last advertised.

`update_peer_instance` runs on every re-handshake of a known peer (a reconnect, a
pay-time refresh, a re-introduction). `add_slot` upserts the peer's slot for a given
internal_port and merges each URI (insert if new, refresh otherwise), so it is
idempotent by construction and no longer needs a clear-then-reinsert step (issue
#236). These tests pin that down against the real schema built from
`migrate.create_tables`.
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
class PeerSlotRefreshTests(unittest.TestCase):
    def setUp(self):
        # Real schema, in-memory: swap the singleton's connection so add_slot runs
        # its actual SQL, not a mock.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate.create_tables(conn.cursor())
        conn.commit()
        self.sc = SQLConnection()
        self._orig = SQLConnection._connection
        SQLConnection._connection = conn
        self.conn = conn
        conn.execute("INSERT INTO peer (id, protocol_stack, remote_client_id, gas) "
                     "VALUES (?, ?, '', '0')", (PEER, b""))
        conn.commit()

    def tearDown(self):
        SQLConnection._connection = self._orig

    def _slot(self, port=8080, ip="10.0.0.1"):
        return celaut_pb2.Instance.Uri_Slot(
            internal_port=port,
            uri=[celaut_pb2.Instance.Uri(ip=ip, port=port)],
        )

    def _counts(self):
        s = self.conn.execute("SELECT COUNT(*) FROM slot WHERE peer_id=?", (PEER,)).fetchone()[0]
        u = self.conn.execute(
            "SELECT COUNT(*) FROM uri WHERE slot_id IN (SELECT id FROM slot WHERE peer_id=?)",
            (PEER,)).fetchone()[0]
        return s, u

    def test_readding_the_same_slot_is_idempotent(self):
        # Re-registering the same address on every re-handshake must not pile up rows.
        for _ in range(3):
            self.sc.add_slot(slot=self._slot(), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (1, 1))

    def test_readding_with_a_new_address_accumulates(self):
        # A peer re-registering from a second address keeps the first one too,
        # instead of losing it the way clear-then-reinsert used to.
        self.sc.add_slot(slot=self._slot(ip="10.0.0.1"), peer_id=PEER, transport_protocol=b"tcp")
        self.sc.add_slot(slot=self._slot(ip="10.0.0.2"), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (1, 2))

    def test_a_second_internal_port_gets_its_own_slot(self):
        self.sc.add_slot(slot=self._slot(port=8080), peer_id=PEER, transport_protocol=b"tcp")
        self.sc.add_slot(slot=self._slot(port=9090), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (2, 2))


if __name__ == "__main__":
    unittest.main()
