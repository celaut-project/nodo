"""The agent-facing CLI views of what ``nodo tui`` shows (TUI <-> CLI parity).

Each test seeds the real schema (``src.database.migrate.TABLES``) with a few rows,
runs the command, and checks both renderings: ``--json`` is exactly one JSON
object on one line, and the command's return value is its exit status.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from src.utils.config import ConfigManager  # noqa: E402
from src.database import migrate as migrate_module  # noqa: E402

DATABASE_FILE = ConfigManager().get("DATABASE_FILE")


_BOUND = False


def _bind_commands_to_this_database():
    """Point the commands under test at the database these tests seed.

    A command module reads ``DATABASE_FILE`` once, when it is first imported, and which
    config that is depends on what the rest of the suite imported before it. So the
    commands are bound here rather than trusted to agree with this module's own config.
    """
    global _BOUND
    if _BOUND:
        return
    from src.commands import _catalogue, clients, history, instances, peers

    patchers = [patch.object(module, "DATABASE_FILE", DATABASE_FILE)
                for module in (clients, instances, peers)]
    patchers.append(patch.object(history, "_database",
                                 lambda: _catalogue.connect(DATABASE_FILE)))
    for patcher in patchers:
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
    _BOUND = True


def _fresh_database():
    _bind_commands_to_this_database()
    # Emptied rather than deleted: SQLConnection keeps the file open, and a new
    # file at the same path would be invisible to it.
    os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True)
    connection = sqlite3.connect(DATABASE_FILE)
    with connection, redirect_stdout(io.StringIO()):
        migrate_module.create_tables(connection.cursor())
        for table in migrate_module.TABLES:
            connection.execute(f"DELETE FROM {table}")
    return connection


def _run(function, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        ok = function(*args, **kwargs)
    return ok, out.getvalue()


def _json(output):
    lines = output.strip().splitlines()
    assert len(lines) == 1, output
    return json.loads(lines[0])


class PeerViewTests(unittest.TestCase):
    def setUp(self):
        connection = _fresh_database()
        with connection:
            connection.execute(
                "INSERT INTO peer (id, balance_mu, reputation_score, reputation_index, "
                "local_client_id) VALUES ('peer-1', 'x', 3, 1, 'client-9')")
            connection.execute("INSERT INTO uri (peer_id, ip, port) VALUES ('peer-1', '10.0.0.2', 8090)")
            connection.execute(
                "INSERT INTO payments (direction, status, peer_id, amount_mu, created_at) "
                "VALUES ('out', 'accepted', 'peer-1', '1000', '2026-01-01 00:00:00')")
            connection.execute(
                "INSERT INTO reputation_events (subject_kind, subject_id, amount, reason, score_after) "
                "VALUES ('peer', 'peer-1', 3, 'payment_communicated', 3)")
        connection.close()
        from src.commands import peers
        self.peers = peers

    def test_json_lists_every_peer(self):
        ok, out = _run(self.peers.peers_command, ["--json"])
        self.assertTrue(ok)
        report = _json(out)
        self.assertEqual([p["id"] for p in report["peers"]], ["peer-1"])
        peer = report["peers"][0]
        self.assertEqual(peer["endpoints"], ["10.0.0.2:8090"])
        self.assertEqual(peer["reputation_score"], 3)
        self.assertEqual(peer["local_client_id"], "client-9")
        # An unconvertible balance is None, never 0.
        self.assertIsNone(peer["balance_mu"])
        self.assertNotIn("payments", peer)

    def test_one_peer_carries_the_detail_card(self):
        ok, out = _run(self.peers.peers_command, ["peer-1", "--json"])
        self.assertTrue(ok)
        peer = _json(out)["peer"]
        self.assertEqual([p["amount_mu"] for p in peer["payments"]], ["1000"])
        self.assertEqual([e["reason"] for e in peer["reputation_events"]], ["payment_communicated"])

    def test_unknown_peer_fails(self):
        ok, out = _run(self.peers.peers_command, ["nobody", "--json"])
        self.assertFalse(ok)
        self.assertIn("error", _json(out))

    def test_text_listing_still_prints(self):
        ok, out = _run(self.peers.peers_command, [])
        self.assertTrue(ok)
        self.assertIn("ID: peer-1", out)
        self.assertIn("Endpoints: 10.0.0.2:8090", out)

    def test_reputation_adjustment_writes_score_and_event(self):
        ok, out = _run(self.peers.adjust_peer_reputation, "peer-1", "-2", as_json=True)
        self.assertTrue(ok)
        self.assertEqual(_json(out)["reputation_score"], 1)
        connection = sqlite3.connect(DATABASE_FILE)
        try:
            reasons = [r[0] for r in connection.execute(
                "SELECT reason FROM reputation_events WHERE subject_id = 'peer-1' ORDER BY id")]
        finally:
            connection.close()
        self.assertEqual(reasons[-1], "operator_adjustment")

    def test_reputation_adjustment_refuses_bad_input(self):
        for peer, delta in (("peer-1", "abc"), ("peer-1", "0"), ("nobody", "+1")):
            ok, out = _run(self.peers.adjust_peer_reputation, peer, delta, as_json=True)
            self.assertFalse(ok, (peer, delta))
            self.assertIn("error", _json(out))


class ClientViewTests(unittest.TestCase):
    def setUp(self):
        connection = _fresh_database()
        with connection:
            connection.execute("INSERT INTO clients (id, balance_mu, unmetered) VALUES ('client-1', '500', 1)")
            connection.execute(
                "INSERT INTO deposit_tokens (id, client_id, status, created_at) "
                "VALUES ('tok-1', 'client-1', 'payed', '2026-01-03 09:59:00')")
            connection.execute(
                "INSERT INTO payments (direction, status, client_id, amount_mu, created_at) "
                "VALUES ('in', 'accepted', 'client-1', '500', '2026-01-03 10:00:00')")
            connection.execute("INSERT INTO peer (id, local_client_id) VALUES ('peer-7', 'client-1')")
        connection.close()
        from src.commands import clients
        self.clients = clients

    def test_json_lists_clients_with_raw_balance(self):
        ok, out = _run(self.clients.clients_command, ["--json"])
        self.assertTrue(ok)
        client = _json(out)["clients"][0]
        self.assertEqual(client["balance_mu"], 500)
        self.assertTrue(client["unmetered"])

    def test_one_client_carries_the_detail_card(self):
        ok, out = _run(self.clients.clients_command, ["client-1", "--json"])
        self.assertTrue(ok)
        client = _json(out)["client"]
        self.assertEqual(client["bound_peer_id"], "peer-7")
        self.assertEqual([t["id"] for t in client["deposit_tokens"]], ["tok-1"])
        self.assertEqual(len(client["payments"]), 1)

    def test_limit_must_be_positive(self):
        ok, out = _run(self.clients.clients_command, ["client-1", "--limit", "0", "--json"])
        self.assertFalse(ok)


class HistoryViewTests(unittest.TestCase):
    def setUp(self):
        connection = _fresh_database()
        with connection:
            for status, amount in (("accepted", "700"), ("accepted", "300"), ("rejected", "50")):
                connection.execute(
                    "INSERT INTO payments (direction, status, ledger, amount_mu, created_at) "
                    "VALUES ('in', ?, 'ergo', ?, datetime('now'))", (status, amount))
            connection.execute(
                "INSERT INTO payments (direction, status, ledger, amount_mu, created_at) "
                "VALUES ('in', 'accepted', 'ergo', '5', '2000-01-01 00:00:00')")
            connection.execute(
                "INSERT INTO payments (direction, status, ledger, amount_mu) "
                "VALUES ('out', 'accepted', 'ergo', '999')")
            connection.execute(
                "INSERT INTO energy_consumption (timestamp, energy_joules, watts, price_per_kwh, "
                "currency, backend, is_floor) VALUES (datetime('now'), 3.6e6, 42.0, 0.2, 'USD', 'rapl', 0)")
            connection.execute(
                "INSERT INTO demand_history (hour, instances_held, refused_closed) "
                "VALUES (strftime('%Y-%m-%dT', 'now', 'localtime') || '07', 4, 2)")
        connection.close()
        from src.commands import history
        self.history = history

    def test_earnings_windows_count_only_accepted_incoming(self):
        ok, out = _run(self.history.earnings, ["--json"])
        self.assertTrue(ok)
        (ergo,) = _json(out)["earnings"]
        self.assertEqual(ergo["ledger"], "ergo")
        self.assertEqual(ergo["day_mu"], 1000)
        self.assertEqual(ergo["total_mu"], 1005)
        self.assertEqual(ergo["refused_mu"], 50)

    def test_energy_reports_latest_and_hourly(self):
        ok, out = _run(self.history.energy, ["--json", "--hours", "24"])
        self.assertTrue(ok)
        report = _json(out)
        self.assertEqual(report["latest"]["watts"], 42.0)
        self.assertEqual(len(report["hourly"]), 1)
        self.assertAlmostEqual(report["hourly"][0]["cost"], 0.2)
        self.assertIn("PRICE_PER_KWH", report["config"])

    def test_schedule_folds_demand_onto_the_clock(self):
        ok, out = _run(self.history.schedule, ["--json"])
        self.assertTrue(ok)
        report = _json(out)
        self.assertEqual(report["demand_by_hour"]["held"][7], 4)
        self.assertEqual(report["demand_by_hour"]["refused"][7], 2)
        self.assertFalse(report["enabled"])
        self.assertTrue(report["open_now"])

    def test_bad_window_is_refused(self):
        ok, _ = _run(self.history.energy, ["--hours", "nope"])
        self.assertFalse(ok)


class InstanceViewTests(unittest.TestCase):
    def test_json_carries_raw_values_beside_display_strings(self):
        connection = _fresh_database()
        with connection:
            connection.execute("INSERT INTO clients (id, balance_mu) VALUES ('client-1', '0')")
            connection.execute(
                "INSERT INTO local_instances (id, name, father_id, balance_mu, service_id, mem_limit) "
                "VALUES ('inst-1', 'web', 'client-1', '1234', 'svc-1', 1048576)")
        connection.close()
        from src.commands import instances
        with patch.object(instances, "_prune_stale_instances"), \
                patch.object(instances, "_has_runtime_snapshot", return_value=False):
            ok, out = _run(instances.list_instances, as_json=True)
        self.assertTrue(ok)
        (record,) = _json(out)["instances"]
        self.assertEqual(record["id"], "inst-1")
        self.assertEqual(record["balance_mu"], 1234)
        self.assertEqual(record["mem_limit_bytes"], 1048576)
        self.assertEqual(record["parent_type"], "client")
        # Unreadable live counters are None, never 0.
        self.assertIsNone(record["usage"]["net_rx_bytes"])


class ChatJsonTests(unittest.TestCase):
    def test_threads_and_messages_as_json(self):
        from src.commands import chat
        threads = [{"id": "c1", "peer_id": "p", "opened_by_us": 1, "topic": "t",
                    "opened_at": "2026-01-01", "closed_at": None}]
        messages = [{"ts": 1, "from_us": 1, "body": "hi", "conversation_id": "c1"}]
        # A stand-in for src.manager.chat, which needs the node's runtime to import.
        fake = types.ModuleType("src.manager.chat")
        fake.list_conversations = lambda peer_id=None: threads
        fake.get_conversation_history = lambda conversation_id, limit: messages
        with patch.dict(sys.modules, {"src.manager.chat": fake}):
            ok, out = _run(chat.list_threads, as_json=True)
            self.assertTrue(ok)
            self.assertEqual(_json(out)["conversations"], threads)
            ok, out = _run(chat.show_thread, "c1", as_json=True)
        self.assertEqual(_json(out)["messages"], messages)


class StatusTests(unittest.TestCase):
    def test_json_report_is_one_object_and_never_fails_on_a_down_node(self):
        _fresh_database().close()
        from src.commands import status
        with patch("src.commands.daemon.is_serving", return_value=False), \
                patch("src.utils.operator_alerts.collect", return_value=[]):
            ok, out = _run(status.status, ["--json"])
        self.assertTrue(ok)
        report = _json(out)
        self.assertIs(report["serving"], False)
        for key in ("node_id", "gateway_port", "address", "alerts", "counts", "host"):
            self.assertIn(key, report)
        self.assertEqual(report["counts"]["peers"], 0)
        self.assertNotIn("wallet", report)


class LogsAndDocsTests(unittest.TestCase):
    def test_bounded_tail(self):
        from src.commands import logs
        with tempfile.TemporaryDirectory() as main_dir:
            os.makedirs(os.path.join(main_dir, "storage"))
            with open(os.path.join(main_dir, "storage", "app.log"), "w") as f:
                f.write("".join(f"line {i}\n" for i in range(10)))
            ok, out = _run(logs.logs, main_dir, ["-n", "3", "--json"])
        self.assertTrue(ok)
        self.assertEqual(_json(out)["lines"], ["line 7", "line 8", "line 9"])

    def test_docs_list_and_page(self):
        from src.commands import docs
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ok, out = _run(docs.docs, root, ["--json"])
        self.assertTrue(ok)
        pages = [p["page"] for p in _json(out)["pages"]]
        self.assertIn("USAGE.md", pages)
        ok, out = _run(docs.docs, root, ["usage"])
        self.assertTrue(ok)
        self.assertIn("User Guide", out)

    def test_docs_never_leave_the_folder(self):
        from src.commands import docs
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ok, _ = _run(docs.docs, root, ["../README"])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
