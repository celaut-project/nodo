"""Re-registering a known peer must not duplicate its slot/uri rows.

`update_peer_instance` now runs on every re-handshake of a known peer (a
reconnect, a pay-time refresh, a re-introduction). `add_slot` is a plain INSERT,
so without clearing first the `slot` and `uri` tables grow by a full set on each
call. `clear_peer_slots` resets them so the refresh is idempotent, which these
tests pin down against the real schema built from `migrate.create_tables`.
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
        # Real schema, in-memory: swap the singleton's connection so add_slot /
        # clear_peer_slots run their actual SQL, not a mock.
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

    def test_add_slot_alone_is_not_idempotent(self):
        # Documents the root cause: two adds of the same slot pile up two rows.
        self.sc.add_slot(slot=self._slot(), peer_id=PEER, transport_protocol=b"tcp")
        self.sc.add_slot(slot=self._slot(), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (2, 2))

    def test_clear_then_readd_keeps_a_single_set(self):
        # The refresh pattern update_peer_instance now uses.
        for _ in range(3):
            self.sc.clear_peer_slots(peer_id=PEER)
            self.sc.add_slot(slot=self._slot(), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (1, 1))

    def test_clear_removes_slots_and_their_uris(self):
        self.sc.add_slot(slot=self._slot(), peer_id=PEER, transport_protocol=b"tcp")
        self.assertEqual(self._counts(), (1, 1))
        self.sc.clear_peer_slots(peer_id=PEER)
        self.assertEqual(self._counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
