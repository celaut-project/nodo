"""Peer registration under node identity (#236): what a signed announcement may and
may not do to our stored view of a peer.

These pin the defects found reviewing the first cut of the implementation:
a relayed announcement with a swapped payment contract, a legacy uuid-keyed peer
losing its gas on upgrade, and a superseded address never being dropped.
"""
import os
import sqlite3
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from mnemonic import Mnemonic

    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import node_identity as ni
    from src.reputation_system.bip_wallet_verification import (
        bip_ecdsa_sign,
        derive_compressed_pubkey,
    )
    import src.manager.manager as manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerIdentityRegistrationTests(unittest.TestCase):
    def setUp(self):
        # A file-backed DB: _known_peer_id goes through query_interface.fetch_query,
        # which opens its own connection to DATABASE_FILE and cannot see an
        # in-memory one.
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self._orig_db = manager.sc  # keep the module-level singleton reference
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        migrate.create_tables(self.conn.cursor())
        self.conn.commit()
        self._orig_conn = SQLConnection._connection
        SQLConnection._connection = self.conn

        import src.database.query_interface as qi
        self._orig_qi_db = qi.DATABASE_FILE
        qi.DATABASE_FILE = self.db_path

        self.mnemonic = Mnemonic("english").generate(strength=128)
        self.pubkey = derive_compressed_pubkey(self.mnemonic).hex()

    def tearDown(self):
        import src.database.query_interface as qi
        qi.DATABASE_FILE = self._orig_qi_db
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def _peer(self, uris, *, signed=True, ts=100, seq=1, contract=b"HONEST"):
        peer = celaut_pb2.Peer()
        for ip, port in uris:
            slot = peer.instance.uri_slot.add()
            slot.internal_port = 9999
            slot.uri.add(ip=ip, port=port)
        api_slot = peer.instance.api.slot.add()
        api_slot.port = 9999
        api_slot.transport.tags.append("tcp")
        gas_price = peer.instance.api.payment_contracts.add()
        gas_price.contract.ledger.formal = contract
        gas_price.gas_amount.n = "1"
        if signed:
            peer.public_key, peer.ts, peer.seq = self.pubkey, ts, seq
            peer.signature = bip_ecdsa_sign(
                self.mnemonic,
                ni.canonical_peer_payload(
                    self.pubkey, ts, seq, ni.canonical_instance_digest(peer.instance), 0
                ),
            )
        return peer

    def _uris(self, peer_id):
        return sorted(
            row[0] for row in self.conn.execute(
                "SELECT u.ip FROM uri u JOIN slot s ON u.slot_id = s.id WHERE s.peer_id = ?",
                (peer_id,),
            ).fetchall()
        )

    def test_signed_peer_is_identified_by_its_public_key(self):
        self.assertEqual(manager.add_peer_instance(self._peer([("10.0.0.1", 9999)])), self.pubkey)

    def test_swapped_payment_contract_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.instance.api.payment_contracts[0].contract.ledger.formal = b"ATTACKER"
        self.assertIsNone(manager._verified_peer_public_key(peer))

    def test_injected_address_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.instance.uri_slot[0].uri.add(ip="6.6.6.6", port=9999)
        self.assertIsNone(manager._verified_peer_public_key(peer))

    def test_legacy_uuid_peer_is_adopted_keeping_its_state(self):
        legacy = manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], signed=False))
        self.assertNotEqual(legacy, self.pubkey)
        self.conn.execute(
            "UPDATE peer SET gas='999999', remote_client_id='client-abc' WHERE id=?", (legacy,)
        )
        self.conn.commit()

        self.assertEqual(
            manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], ts=200)), self.pubkey
        )
        rows = self.conn.execute("SELECT id, gas, remote_client_id FROM peer").fetchall()
        self.assertEqual(len(rows), 1, "the legacy row must be adopted, not duplicated")
        self.assertEqual(rows[0][0], self.pubkey)
        self.assertEqual(rows[0][1], "999999")
        self.assertEqual(rows[0][2], "client-abc")

    def test_a_newer_signed_announcement_drops_superseded_addresses(self):
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100, seq=1))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200, seq=2))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_several_addresses_in_one_announcement_are_all_kept(self):
        manager.add_peer_instance(
            self._peer([("5.6.7.8", 9999), ("9.9.9.9", 9999)], ts=100, seq=1)
        )
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8", "9.9.9.9"])

    def test_a_replayed_announcement_cannot_prune_addresses(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200, seq=2))
        # An attacker replays an older, genuinely signed message to push the peer
        # back to a stale address.
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100, seq=1))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_registration_is_idempotent(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=100, seq=1))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200, seq=2))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM slot WHERE peer_id=?", (self.pubkey,)
            ).fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
