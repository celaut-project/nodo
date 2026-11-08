"""``nodo statement`` and ``nodo receipt hops|verify --statement`` (#419, #631)."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from src.commands import receipt as receipt_command
    from src.commands import statement as st
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.identity import execution_receipt as er
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Keep:
    """The connection of a test, that a command may close without closing the test's."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        pass


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StatementCommandTests(unittest.TestCase):

    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()
        SQLConnection._connection = self.connection
        self.addCleanup(self._restore)
        patcher = patch.object(st, "_open", side_effect=lambda: _Keep(self.connection))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sc.add_client("client-one", 1000, None)
        self.sc.add_local_instance(
            father_id="client-one", container_ip="10.0.0.2", container_id="tok-web", name="web",
            balance_mu=500, serialized_instance="", service_id="ab" * 32, virtualizer="ch",
            disk_space=0, envs="", movements=(ledger.Movement(ledger.START, 500),))
        self.sc.move_balance("tok-web", -7, ledger.TUNNEL, detail={
            "execution_name": "ee" * 32, "session": "ab", "open": True})
        self.sc.record_tunnel_close("tok-web", {
            "execution_name": "ee" * 32, "session": "ab", "close": True,
            "receipt_digest": "cd" * 32, "mu_charged": "7"})

    def _restore(self):
        SQLConnection._connection = self._original
        self.connection.close()

    def run_command(self, argv, module=st, function="statement_command"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = getattr(module, function)(argv)
        return ok, out.getvalue()

    def json_of(self, argv, **kwargs):
        ok, text = self.run_command(argv, **kwargs)
        self.assertEqual(len(text.strip().splitlines()), 1, "one object on one line")
        return ok, json.loads(text)

    def test_the_accounts_are_listed_with_their_head_and_no_token(self):
        ok, document = self.json_of(["--json"])

        self.assertTrue(ok)
        by_label = {a["label"]: a for a in document["accounts"]}
        self.assertEqual(set(by_label), {"client-one", "web"})
        web = by_label["web"]
        self.assertEqual((web["kind"], web["alive"], web["balance_mu"], web["head_seq"]),
                         ("local", True, "493", 3))
        self.assertEqual(web["ref"], ledger.account_ref("tok-web"))
        self.assertEqual(web["father_ref"], ledger.account_ref("client-one"))
        self.assertNotIn("tok-web", json.dumps(document))

    def test_an_account_is_named_by_label_reference_or_token(self):
        ref = ledger.account_ref("tok-web")
        for name in ("web", ref, ref[:8], "tok-web"):
            with self.subTest(name=name):
                ok, document = self.json_of([name, "--json"])
                self.assertTrue(ok)
                self.assertEqual(document["account"]["label"], "web")

    def test_the_movements_come_oldest_first_with_the_chain_check(self):
        ok, document = self.json_of(["web", "--json"])

        self.assertEqual([m["seq"] for m in document["movements"]], [1, 2, 3])
        self.assertEqual(document["chain"], {
            "ok": True, "problem": "", "seq": 0, "checked": 3, "partial": False})
        self.assertFalse(document["has_more"])
        anchor = document["movements"][2]
        self.assertEqual((anchor["amount_mu"], anchor["detail"]["close"]), ("0", True))
        self.assertNotIn("tok-web", json.dumps(document))

    def test_a_page_and_a_filter(self):
        _, page = self.json_of(["web", "--limit", "2", "--json"])
        self.assertEqual([m["seq"] for m in page["movements"]], [1, 2])
        self.assertTrue(page["has_more"])

        _, rest = self.json_of(["web", "--from", "3", "--json"])
        self.assertEqual([m["seq"] for m in rest["movements"]], [3])

        _, tunnels = self.json_of(["web", "--kind", "tunnel", "--json"])
        self.assertEqual([m["kind"] for m in tunnels["movements"]], ["tunnel", "tunnel"])

    def test_a_chain_that_does_not_hold_is_reported_and_the_command_fails(self):
        self.connection.execute(
            "UPDATE balance_movements SET amount_mu = '-8' WHERE account_id = 'tok-web' "
            "AND seq = 2")
        self.connection.commit()

        ok, document = self.json_of(["web", "--json"])

        self.assertFalse(ok)
        self.assertFalse(document["chain"]["ok"])
        self.assertEqual(document["chain"]["seq"], 2)

    def test_a_chain_that_starts_after_its_first_row_is_partial(self):
        self.connection.execute(
            "DELETE FROM balance_movements WHERE account_id = 'tok-web' AND seq = 1")
        self.connection.commit()

        ok, document = self.json_of(["web", "--json"])

        self.assertTrue(ok)
        self.assertTrue(document["chain"]["partial"])
        self.assertEqual(document["chain"]["checked"], 2)

    def test_an_unknown_account_is_an_error_in_the_chosen_shape(self):
        ok, document = self.json_of(["nobody", "--json"])
        self.assertFalse(ok)
        self.assertIn("nobody", document["error"])
        ok, text = self.run_command(["nobody"])
        self.assertFalse(ok)
        self.assertIn("No account", text)

    def test_a_bad_argument_gives_the_usage(self):
        for argv in (["--limit"], ["--limit", "0"], ["--bogus"], ["a", "b"]):
            with self.subTest(argv=argv):
                ok, text = self.run_command(argv + ["--json"])
                self.assertFalse(ok)
                self.assertIn("error", json.loads(text))

    def test_the_text_form_names_the_chain_and_the_rows(self):
        ok, text = self.run_command(["web"])
        self.assertTrue(ok)
        self.assertIn("Chain: holds together", text)
        self.assertIn("receipt_digest=", text)

    def test_the_faults_are_listed(self):
        self.connection.execute(
            "INSERT INTO statement_faults (peer_id, account_ref, reason, seq, period, ts) "
            "VALUES ('peer', 'ref', 'statement_contradicts_receipt', 4, 0, 100)")
        self.connection.commit()
        ok, document = self.json_of(["faults", "--json"])
        self.assertTrue(ok)
        self.assertEqual(document["faults"], [{
            "ts": 100, "peer_id": "peer", "account_ref": "ref",
            "reason": "statement_contradicts_receipt", "seq": 4}])

    def test_the_hops_show_the_receipt_and_whether_it_is_valid(self):
        key = ed25519.Ed25519PrivateKey.generate()
        peer_id = key.public_key().public_bytes_raw().hex()
        stream = er.ReceiptChain(
            "x", er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL, slot="80",
            signer=lambda r: er.sign_with(r, key), peer_id=peer_id, session="cd" * 8)
        stream.record(er.DIRECTION_IN, b"ping")
        close = stream.checkpoint(mu_charged=15, final=True)
        self.sc.record_descendant_links(peer_id, ["ref-1"])
        self.sc.record_descendant_receipt(peer_id, "ref-1", close.SerializeToString())
        self.sc.record_descendant_links("other-peer", ["ref-2"])
        self.sc.record_descendant_receipt("other-peer", "ref-2", close.SerializeToString())

        ok, document = self.json_of(["hops", "--json"])
        by_peer = {h["peer_id"]: h for h in document["hops"]}

        good = by_peer[peer_id]["receipt"]
        self.assertEqual((good["kind"], good["mu_charged"], good["valid"]), ("close", "15", True))
        self.assertEqual(good["digest"], er.receipt_digest(close))
        # A receipt of one node kept for another one is not valid for it.
        self.assertFalse(by_peer["other-peer"]["receipt"]["valid"])
        self.assertTrue(by_peer["other-peer"]["receipt"]["problems"])
        # The same through ``nodo receipt hops``.
        _, again = self.json_of(["hops", "--json"], module=receipt_command,
                                function="receipt_command")
        self.assertEqual({h["peer_id"] for h in again["hops"]}, set(by_peer))

    def test_check_receipts_against_an_account(self):
        key = ed25519.Ed25519PrivateKey.generate()
        peer_id = key.public_key().public_bytes_raw().hex()
        name = er.execution_name("tok-other", peer_id)
        self.sc.add_client("payer", 1000, None)
        cause = {"execution_name": name, "session": "ab" * 8}
        self.sc.move_balance("payer", -10, ledger.TUNNEL, detail={**cause, "open": True})
        stream = er.ReceiptChain(
            "tok-other", er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL, slot="80",
            signer=lambda r: er.sign_with(r, key), peer_id=peer_id, session="ab" * 8)
        stream.record(er.DIRECTION_IN, b"ping")
        close = stream.checkpoint(mu_charged=10, final=True)
        self.sc.record_tunnel_close("payer", {
            **cause, "close": True, "receipt_digest": er.receipt_digest(close),
            "mu_charged": "10"})

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump([er.to_json_dict(close)], f)
        self.addCleanup(os.unlink, path)

        ok, report = self.json_of(["check-receipts", path, "payer", "--json"])
        self.assertTrue(ok)
        self.assertEqual((report["valid"], report["checked"]), (True, 1))

        # The same through ``nodo receipt verify --statement``.
        ok, verified = self.json_of(["verify", path, "--statement", "payer", "--json"],
                                    module=receipt_command, function="receipt_command")
        self.assertTrue(ok)
        self.assertTrue(verified["statement"]["valid"])

        # A row that holds the digest of another receipt is a contradiction.
        self.connection.execute(
            "UPDATE balance_movements SET detail = REPLACE(detail, ?, ?) "
            "WHERE account_id = 'payer'", (er.receipt_digest(close), "00" * 32))
        self.connection.commit()
        ok, report = self.json_of(["check-receipts", path, "payer", "--json"])
        self.assertFalse(ok)
        self.assertEqual([f["kind"] for f in report["findings"]], ["anchor_digest"])
        ok, verified = self.json_of(["verify", path, "--statement", "payer", "--json"],
                                    module=receipt_command, function="receipt_command")
        self.assertFalse(ok)
        self.assertFalse(verified["valid"])


if __name__ == "__main__":
    unittest.main()
