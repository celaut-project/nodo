"""Tests for persisting an instance's launch env vars in ``local_instances``.

Covers the additive column migration (:func:`src.database.migrate.ensure_columns`)
and the Configuration->JSON serializer used at launch
(:func:`src.gateway.launcher.local_execution.local_execution._serialize_envs`).

Follows the repo convention of guarding imports so the suite skips cleanly when the
runtime dependencies (mnemonic, bee_rpc, protos, netifaces, a loadable config) are
absent, and running fully in CI where they are present.
"""

import sqlite3
import unittest

MIGRATE_IMPORT_ERROR = None
try:
    from src.database.migrate import ensure_columns
except Exception as exc:  # pragma: no cover - environment-dependent
    MIGRATE_IMPORT_ERROR = exc
    ensure_columns = None  # type: ignore[assignment]

SERIALIZE_IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.gateway.launcher.local_execution.local_execution import _serialize_envs
except Exception as exc:  # pragma: no cover - environment-dependent
    SERIALIZE_IMPORT_ERROR = exc
    _serialize_envs = None  # type: ignore[assignment]
    celaut_pb2 = None  # type: ignore[assignment]


# The pre-migration shape of local_instances (no envs column), used to prove the
# additive migration back-fills an existing database.
_OLD_LOCAL_INSTANCES = """
    CREATE TABLE local_instances (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        ip TEXT,
        father_id TEXT,
        balance_mu TEXT,
        mem_limit INTEGER,
        disk_space INTEGER,
        serialized_instance TEXT,
        service_id TEXT,
        virtualizer TEXT DEFAULT NULL
    )
"""


def _columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


@unittest.skipIf(MIGRATE_IMPORT_ERROR is not None, f"Missing deps: {MIGRATE_IMPORT_ERROR}")
class EnsureColumnsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()
        self.cur.execute(_OLD_LOCAL_INSTANCES)

    def tearDown(self):
        self.conn.close()

    def test_adds_missing_column_to_existing_table(self):
        self.assertNotIn("envs", _columns(self.cur, "local_instances"))
        ensure_columns(self.cur, "local_instances", {"envs": "TEXT DEFAULT NULL"})
        self.assertIn("envs", _columns(self.cur, "local_instances"))

    def test_is_idempotent(self):
        ensure_columns(self.cur, "local_instances", {"envs": "TEXT DEFAULT NULL"})
        # Second run must not raise or duplicate the column.
        ensure_columns(self.cur, "local_instances", {"envs": "TEXT DEFAULT NULL"})
        cols = [c for c in _columns(self.cur, "local_instances")]
        self.assertEqual(cols.count("envs"), 1)

    def test_backfilled_column_round_trips_json(self):
        ensure_columns(self.cur, "local_instances", {"envs": "TEXT DEFAULT NULL"})
        self.cur.execute(
            "INSERT INTO local_instances (id, name, envs) VALUES (?, ?, ?)",
            ("cid", "nm", '{"SOURCE_SIGNER_MODE": "seed"}'),
        )
        self.cur.execute("SELECT envs FROM local_instances WHERE id = ?", ("cid",))
        self.assertEqual(self.cur.fetchone()[0], '{"SOURCE_SIGNER_MODE": "seed"}')

    def test_existing_rows_get_null_envs(self):
        self.cur.execute(
            "INSERT INTO local_instances (id, name) VALUES (?, ?)", ("old", "oldname")
        )
        ensure_columns(self.cur, "local_instances", {"envs": "TEXT DEFAULT NULL"})
        self.cur.execute("SELECT envs FROM local_instances WHERE id = ?", ("old",))
        self.assertIsNone(self.cur.fetchone()[0])


@unittest.skipIf(SERIALIZE_IMPORT_ERROR is not None, f"Missing deps: {SERIALIZE_IMPORT_ERROR}")
class SerializeEnvsTests(unittest.TestCase):
    def test_none_config_returns_none(self):
        self.assertIsNone(_serialize_envs(None))

    def test_empty_env_map_returns_none(self):
        self.assertIsNone(_serialize_envs(celaut_pb2.Configuration()))

    def test_serializes_env_map_sorted_json(self):
        cfg = celaut_pb2.Configuration()
        cfg.environment_variables["SOURCE_SIGNER_MODE"] = b"seed"
        cfg.environment_variables["INSTANCE_LABEL"] = b"packer-3"
        out = _serialize_envs(cfg)
        # sort_keys keeps output deterministic regardless of insertion order.
        self.assertEqual(
            out, '{"INSTANCE_LABEL": "packer-3", "SOURCE_SIGNER_MODE": "seed"}'
        )

    def test_a_secret_is_recorded_by_name_and_not_by_value(self):
        """The env of a launched service is where this node's wallet goes.

        A source-application signing with the Ergo seed takes ``SOURCE_MNEMONIC``; a
        bitcoind deriving its own wallet takes ``BITCOIN_MNEMONIC``. Persisted verbatim,
        this column is a second plaintext copy of the mnemonic -- in a different file
        from ``config.yaml``, with different permissions, and in every backup of the
        database.

        The key stays, so the row still answers the question it exists for: *was* this
        instance launched as a signer? What reads it wants a share discriminator
        (``manager.shares.instance_env_values``), never a key.
        """
        cfg = celaut_pb2.Configuration()
        cfg.environment_variables["SOURCE_SIGNER_MODE"] = b"seed"
        cfg.environment_variables["SOURCE_MNEMONIC"] = b"word word word"
        cfg.environment_variables["BITCOIN_MNEMONIC"] = b"twelve other words"
        cfg.environment_variables["BITCOIN_RPC_PASSWORD"] = b"hunter2"
        cfg.environment_variables["BITCOIN_MNEMONIC_PASSPHRASE"] = b"a second secret"
        out = _serialize_envs(cfg)

        for secret in (b"word word word", b"twelve other words", b"hunter2",
                       b"a second secret"):
            self.assertNotIn(secret.decode(), out)
        for name in ("SOURCE_MNEMONIC", "BITCOIN_MNEMONIC", "BITCOIN_RPC_PASSWORD",
                     "BITCOIN_MNEMONIC_PASSPHRASE"):
            self.assertIn(name, out)
        # And the setting beside them is untouched: this is not a blanket redaction.
        self.assertIn('"SOURCE_SIGNER_MODE": "seed"', out)

    def test_secrets_are_matched_by_substring_rather_than_by_a_list(self):
        # A name nobody has thought of yet still has to be caught: the cost of missing
        # one is a wallet in a database, and the cost of over-matching is a setting
        # shown as "<redacted>" in a record nothing reads for settings.
        cfg = celaut_pb2.Configuration()
        cfg.environment_variables["SOME_FUTURE_SERVICE_MNEMONIC_WORDS"] = b"leak me"
        self.assertNotIn("leak me", _serialize_envs(cfg))


if __name__ == "__main__":
    unittest.main()
