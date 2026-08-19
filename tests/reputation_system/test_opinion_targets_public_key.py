"""An opinion is about a node, so it is addressed to that node's public key.

It used to be addressed to the peer's reputation proof (token) id, which made every
published opinion an opinion about *one of that peer's proofs* rather than about the
peer: a single identity key can hold several proofs, and minting a fresh one shed the
reputation others had assigned. Issue #281.

Two consequences are pinned here, because both are silent when wrong: the target we
publish, and *which* peers get published at all -- the old code could only address a
peer whose own proof it had validated, so every other peer we held a score on was
dropped from the opinion set without a word.
"""
import sqlite3
import threading
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    # No config bootstrap here on purpose: it resets the ConfigManager singleton onto a
    # fresh temp dir, and modules that already read DATABASE_FILE into a global would
    # then be pointing at an unmigrated database. Same import shape as
    # ``test_reputation_events``, which skips itself when there is no config.
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.reputation_system.contracts.ergo import transaction as tx_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]

# A peer id IS a public key (issue #236): 33-byte SEC-compressed, 66 hex chars.
PEER_A = "02" + "aa" * 32
PEER_B = "02" + "bb" * 32


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublishedOpinionTargetTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()

        # Bypass __init__ so no singleton opens the node's real database file.
        self.sc = SQLConnection.__new__(SQLConnection)
        patches = [
            mock.patch.object(SQLConnection, "_connection", self.connection),
            mock.patch.object(SQLConnection, "_lock", threading.Lock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.connection.close)

    def _peer(self, peer_id, score=10, index=1000, last_index=0):
        # index far above any submission threshold, so the peer is due for publication.
        self.connection.execute(
            "INSERT INTO peer (id, reputation_score, reputation_index, last_index_on_ledger) "
            "VALUES (?, ?, ?, ?)",
            (peer_id, score, index, last_index),
        )
        self.connection.commit()

    def _submitted(self):
        """Run submit_to_ledger with the ledger stubbed; return what it would publish."""
        captured = []

        def submit(objects):
            captured.extend(objects)
            return True

        self.assertTrue(self.sc.submit_to_ledger(submit=submit, force_submit=True))
        return captured

    def test_the_target_is_the_peers_public_key(self):
        self._peer(PEER_A)

        targets = [target for target, _amount, _json in self._submitted()]

        self.assertIn(PEER_A, targets)

    def test_a_peer_whose_proof_we_never_validated_is_still_published(self):
        """The coverage gap the old proof-id gate caused.

        Neither peer here announced a proof we validated -- under the previous code
        that emptied the opinion set down to the self-entry alone, publishing nothing
        about peers we hold plenty of first-hand score on.
        """
        self._peer(PEER_A, score=10)
        self._peer(PEER_B, score=-100)

        targets = [target for target, _amount, _json in self._submitted()]

        self.assertIn(PEER_A, targets)
        self.assertIn(PEER_B, targets)

    def test_the_self_entry_stays_unaddressed(self):
        """``None`` still means "ourselves"; transaction.py fills in our own key."""
        self._peer(PEER_A)

        targets = [target for target, _amount, _json in self._submitted()]

        self.assertEqual(targets.count(None), 1)

    def test_the_amount_is_shared_out_by_score(self):
        """A peer's share of the token still tracks its score, not its proof."""
        self._peer(PEER_A, score=30)
        self._peer(PEER_B, score=10)

        amounts = {
            target: amount for target, amount, _json in self._submitted()
            if target is not None
        }

        self.assertGreater(amounts[PEER_A], amounts[PEER_B])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SelfOpinionTargetTests(unittest.TestCase):
    """The self-opinion must be addressed to our own key, not to our proof id.

    Checked by reference rather than by building a box, since that needs a JVM: the
    regression to guard against is R5 falling back to ``proof_id`` again, and the
    name it must reach for instead is the identity helper. Same approach as
    ``test_reputation_alignment``.
    """

    def test_the_tx_builder_addresses_itself_by_identity_key(self):
        names = tx_module.__dict__["__create_reputation_proof_tx"].__code__.co_names
        self.assertIn("get_node_public_key_hex", names)


if __name__ == "__main__":
    unittest.main()
