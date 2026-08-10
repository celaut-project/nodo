"""Peer registration under node identity (#236): what a signed announcement may and
may not do to our stored view of a peer.

These pin the defects found reviewing the first cut of the implementation:
a relayed announcement with a swapped payment contract, a legacy uuid-keyed peer
losing its balance on upgrade, and a superseded address never being dropped.
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

    def _peer(self, uris, *, signed=True, ts=100, contract=b"HONEST", transport="tcp"):
        peer = celaut_pb2.Peer()
        for ip, port in uris:
            uri = peer.uri.add(ip=ip, port=port)
            uri.transport.tags.append(transport)
        mu_per_unit = peer.payment_contracts.add()
        mu_per_unit.contract.ledger.formal = contract
        mu_per_unit.mu_per_unit.n = "1"
        if signed:
            peer.public_key, peer.ts = self.pubkey, ts
            peer.signature = bip_ecdsa_sign(
                self.mnemonic,
                ni.canonical_peer_payload(
                    self.pubkey, ts, ni.canonical_peer_content_digest(peer)
                ),
            )
        return peer

    def _uris(self, peer_id):
        return sorted(
            row[0] for row in self.conn.execute(
                "SELECT ip FROM uri WHERE peer_id = ?", (peer_id,)
            ).fetchall()
        )

    def test_signed_peer_is_identified_by_its_public_key(self):
        self.assertEqual(manager.add_peer_instance(self._peer([("10.0.0.1", 9999)])), self.pubkey)

    def test_swapped_payment_contract_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.payment_contracts[0].contract.ledger.formal = b"ATTACKER"
        self.assertIsNone(manager._verified_peer_public_key(peer))

    def test_injected_address_is_rejected(self):
        peer = self._peer([("10.0.0.1", 9999)])
        peer.uri.add(ip="6.6.6.6", port=9999)
        self.assertIsNone(manager._verified_peer_public_key(peer))

    def test_downgraded_transport_is_rejected(self):
        # Flipping an address's transport would send a reader at it with the wrong
        # kind of socket, so it must not survive the signature check.
        peer = self._peer([("10.0.0.1", 9999)])
        del peer.uri[0].transport.tags[:]
        peer.uri[0].transport.tags.append("udp")
        self.assertIsNone(manager._verified_peer_public_key(peer))

    def test_an_unsigned_message_cannot_hijack_an_identified_peer(self):
        # A peer's addresses are public (GetPeerInfo serves them, and they go
        # on-chain). Naming one must not let a stranger rewrite that peer's
        # advertisement or payment contracts without signing for its identity.
        self.assertEqual(
            manager.add_peer_instance(self._peer([("10.0.0.1", 9999)])), self.pubkey
        )
        honest = self.conn.execute(
            "SELECT advertisement FROM peer WHERE id=?", (self.pubkey,)
        ).fetchone()[0]

        forged = self._peer([("10.0.0.1", 9999)], signed=False, contract=b"ATTACKER")
        forged.public_key, forged.signature = self.pubkey, "not-a-signature"
        manager.add_peer_instance(forged)

        self.assertEqual(
            self.conn.execute(
                "SELECT advertisement FROM peer WHERE id=?", (self.pubkey,)
            ).fetchone()[0],
            honest,
            "an unsigned announcement overwrote a verified peer's advertisement",
        )

    def test_an_address_whose_transport_is_unusable_is_not_kept(self):
        # Storing it would leave a row the node can never speak to, and pruning must
        # not resurrect it just because the announcement listed it.
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=100))
        manager.add_peer_instance(
            self._peer([("5.6.7.8", 9999)], ts=200, transport="carrier-pigeon")
        )
        self.assertEqual(self._uris(self.pubkey), [])

    def test_the_declared_transport_is_stored(self):
        peer_id = manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], transport="udp"))
        self.assertEqual(
            self.conn.execute(
                "SELECT transport FROM uri WHERE peer_id = ?", (peer_id,)
            ).fetchone()[0],
            "udp",
        )

    def test_legacy_uuid_peer_is_adopted_keeping_its_state(self):
        legacy = manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], signed=False))
        self.assertNotEqual(legacy, self.pubkey)
        self.conn.execute(
            "UPDATE peer SET balance_mu='999999', remote_client_id='client-abc' WHERE id=?", (legacy,)
        )
        self.conn.commit()

        self.assertEqual(
            manager.add_peer_instance(self._peer([("10.0.0.1", 9999)], ts=200)), self.pubkey
        )
        rows = self.conn.execute("SELECT id, balance_mu, remote_client_id FROM peer").fetchall()
        self.assertEqual(len(rows), 1, "the legacy row must be adopted, not duplicated")
        self.assertEqual(rows[0][0], self.pubkey)
        self.assertEqual(rows[0][1], "999999")
        self.assertEqual(rows[0][2], "client-abc")

    def test_a_newer_signed_announcement_drops_superseded_addresses(self):
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_several_addresses_in_one_announcement_are_all_kept(self):
        manager.add_peer_instance(
            self._peer([("5.6.7.8", 9999), ("9.9.9.9", 9999)], ts=100)
        )
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8", "9.9.9.9"])

    def test_a_replayed_announcement_cannot_prune_addresses(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        # An attacker replays an older, genuinely signed message to push the peer
        # back to a stale address.
        manager.add_peer_instance(self._peer([("1.2.3.4", 9999)], ts=100))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])

    def test_registration_is_idempotent(self):
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=100))
        manager.add_peer_instance(self._peer([("5.6.7.8", 9999)], ts=200))
        self.assertEqual(self._uris(self.pubkey), ["5.6.7.8"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM uri WHERE peer_id=?", (self.pubkey,)
            ).fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
