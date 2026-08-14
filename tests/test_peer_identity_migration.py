"""Peer rows not keyed by an identity public key must be dropped on migration.

A peer's id IS the public key that signed its announcement (issue #236), so the random
`uuid4` ids handed out before node identity existed can never be refreshed again:
`accept_peer_refresh` compares the verified key against the row's id and always refuses,
`maintain` skips the row on every pass, and `submit_to_ledger` keeps republishing it
on-chain. `migrate.drop_unidentified_peers` is the one-time cleanup; these tests pin it
against the real schema, including that it leaves a properly keyed peer alone.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from src.database import migrate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    migrate = None  # type: ignore[assignment]

# Kept apart from the import above: node_identity reaches BIP-32 key derivation, which
# needs a compiled dependency the migration itself does not. Only the one test that
# cross-checks the two rules skips when it is missing.
IDENTITY_IMPORT_ERROR = None
try:
    from src.reputation_system.node_identity import normalize_public_key_hex
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IDENTITY_IMPORT_ERROR = import_exc

SIGNED = "02" + "ab" * 32
LEGACY = "e3f1a0c2-1b2d-4e5a-8c7b-9d0e1f2a3b4c"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DropUnidentifiedPeersTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        migrate.create_tables(self.conn.cursor())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _add_peer(self, peer_id: str):
        """One peer plus a row in every table that names it, as a live peer would have."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO peer (id, balance_mu) VALUES (?, '100')", (peer_id,))
        cur.execute(
            "INSERT INTO uri (peer_id, ip, port) VALUES (?, '10.0.0.1', 8080)", (peer_id,)
        )
        cur.execute(
            "INSERT INTO contract_instance (address, ledger_hash, contract_hash, peer_id, "
            "mu_per_unit) VALUES (?, 'ledger', 'contract', ?, '1')", (peer_id, peer_id)
        )
        cur.execute(
            "INSERT INTO delegated_instances (token_delegation, peer_id) VALUES (?, ?)",
            (f"delegation-{peer_id[:8]}", peer_id),
        )
        cur.execute(
            "INSERT INTO forced_execution_peer (token, peer_id) VALUES (?, ?)",
            (f"forced-{peer_id[:8]}", peer_id),
        )
        self.conn.commit()

    def _peers(self):
        return {row[0] for row in self.conn.execute("SELECT id FROM peer")}

    def _dependents_of(self, peer_id: str) -> int:
        return sum(
            self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE peer_id = ?", (peer_id,)
            ).fetchone()[0]
            for table in migrate._PEER_DEPENDENTS
        )

    def test_a_uuid_keyed_peer_and_everything_hanging_off_it_goes(self):
        self._add_peer(LEGACY)
        migrate.drop_unidentified_peers(self.conn.cursor())
        self.conn.commit()
        self.assertEqual(self._peers(), set())
        # Left behind, these would be rows pointing at a peer that no longer exists --
        # and delegated_instances/forced_execution_peer are the two remove_peer forgets.
        self.assertEqual(self._dependents_of(LEGACY), 0)

    def test_a_public_key_keyed_peer_is_untouched(self):
        self._add_peer(SIGNED)
        migrate.drop_unidentified_peers(self.conn.cursor())
        self.conn.commit()
        self.assertEqual(self._peers(), {SIGNED})
        self.assertEqual(self._dependents_of(SIGNED), len(migrate._PEER_DEPENDENTS))
        # Its balance is the thing a wrong sweep would destroy.
        self.assertEqual(
            self.conn.execute("SELECT balance_mu FROM peer WHERE id = ?", (SIGNED,)).fetchone()[0],
            "100",
        )

    def test_only_the_unidentified_peer_goes_when_both_are_present(self):
        self._add_peer(SIGNED)
        self._add_peer(LEGACY)
        migrate.drop_unidentified_peers(self.conn.cursor())
        self.conn.commit()
        self.assertEqual(self._peers(), {SIGNED})
        self.assertEqual(self._dependents_of(SIGNED), len(migrate._PEER_DEPENDENTS))

    @unittest.skipIf(
        IDENTITY_IMPORT_ERROR is not None, f"Missing bip32: {IDENTITY_IMPORT_ERROR}"
    )
    def test_the_sql_predicate_agrees_with_normalize_public_key_hex(self):
        """The SQL rule must accept exactly the ids `add_peer_instance` can produce.

        A peer id comes straight from `normalize_public_key_hex`, so anything that
        function rejects is unidentified and anything it returns unchanged must survive --
        otherwise migration deletes live peers, or leaves unrefreshable ones behind.
        """
        candidates = [
            SIGNED,
            "03" + "0f" * 32,
            "02" + "AB" * 32,      # uppercase: not the canonical spelling
            "02" + "ab" * 31,      # 64 chars
            "02" + "ab" * 32 + "ff",
            "02" + "ag" * 32,      # 'g' is not hex
            LEGACY,
            "",
        ]
        cur = self.conn.cursor()
        for candidate in candidates:
            cur.execute("INSERT INTO peer (id) VALUES (?)", (candidate,))
        self.conn.commit()
        flagged = {
            row[0] for row in
            self.conn.execute(f"SELECT id FROM peer WHERE {migrate._UNIDENTIFIED_PEER}")
        }
        for candidate in candidates:
            canonical = normalize_public_key_hex(candidate) == candidate
            self.assertEqual(
                candidate not in flagged, canonical,
                f"{candidate!r}: SQL and normalize_public_key_hex disagree",
            )

    def test_it_is_idempotent_and_a_no_op_on_a_clean_database(self):
        self._add_peer(SIGNED)
        self._add_peer(LEGACY)
        cur = self.conn.cursor()
        for _ in range(3):
            migrate.drop_unidentified_peers(cur)
            self.conn.commit()
            self.assertEqual(self._peers(), {SIGNED})

    def test_a_full_migration_pass_drops_it(self):
        """The startup path, not just the function: `migrate` runs `create_tables`."""
        self._add_peer(LEGACY)
        migrate.create_tables(self.conn.cursor())
        self.conn.commit()
        self.assertEqual(self._peers(), set())


if __name__ == "__main__":
    unittest.main()
