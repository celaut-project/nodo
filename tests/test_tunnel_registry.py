"""The tunnel registry and the commands that read it: ``nodo tunnels``,
``nodo tunnel_close`` and ``nodo tunnel --detach``.

Real processes stand in for tunnels -- a ``python -c`` that sleeps with ``tunnel``
in its argv, which is all the registry checks -- so signalling, sweeping and the
detach handshake all run for real without a node.
"""

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src.commands import completion
from src.commands import tunnels as tunnels_command
from src.utils import tunnel_registry as registry

try:
    from src.commands import tunnel as tunnel_command
    TUNNEL_IMPORT_ERROR = None
except Exception as import_exc:  # pragma: no cover - environment-dependent
    tunnel_command = None
    TUNNEL_IMPORT_ERROR = import_exc

try:
    from src.commands import kill as kill_command
    KILL_IMPORT_ERROR = None
except Exception as import_exc:  # pragma: no cover - environment-dependent
    kill_command = None
    KILL_IMPORT_ERROR = import_exc

# A process the registry accepts as a tunnel. TERM_IGNORED makes it outlive SIGTERM.
_SLEEPER = "import signal, sys, time\n{ignore}\ntime.sleep(60)\n"
TERM_IGNORED = "signal.signal(signal.SIGTERM, signal.SIG_IGN)"


def _sleeper(ignore_term: bool = False) -> subprocess.Popen:
    code = _SLEEPER.format(ignore=TERM_IGNORED if ignore_term else "")
    process = subprocess.Popen([sys.executable, "-c", code, "tunnel"])
    time.sleep(0.2)  # let it install its handler before anyone signals it
    return process


def _record(tunnel_id: str, pid: int, **overrides):
    record = registry.new_record(
        tunnel_id=tunnel_id, instance="my-instance", token="abcdef0123456789",
        slot=8080, udp=False, listen_host="127.0.0.1", listen_port=9000,
        gateway="127.0.0.1:8090", peer=None, detached=False, pid=pid,
    )
    record.update(overrides)
    return record


def _run_json(function, *args, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ok = function(*args, as_json=True, **kwargs)
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"--json must print exactly one line, got {lines!r}"
    return ok, json.loads(lines[0])


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nodo-tunnels-")
        self.env = patch.dict(os.environ, {registry.DIR_ENV: self.directory})
        self.env.start()
        self.processes = []

    def tearDown(self):
        self.env.stop()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()

    def spawn(self, ignore_term: bool = False) -> subprocess.Popen:
        process = _sleeper(ignore_term)
        self.processes.append(process)
        return process


class RegistryTests(RegistryTestCase):
    def test_a_registered_tunnel_is_listed_with_its_age(self):
        process = self.spawn()
        registry.register(_record("aaaa1111", process.pid))

        listed = registry.list_tunnels()

        self.assertEqual([record["id"] for record in listed], ["aaaa1111"])
        self.assertEqual(listed[0]["listen_port"], 9000)
        self.assertIsInstance(listed[0]["age_secs"], int)

    def test_a_tunnel_whose_process_is_gone_is_swept_not_listed(self):
        process = self.spawn()
        registry.register(_record("dead0000", process.pid))
        process.kill()
        process.wait()

        self.assertEqual(registry.list_tunnels(), [])
        self.assertFalse(os.path.exists(registry.record_path("dead0000")))

    def test_a_half_written_or_foreign_file_is_ignored(self):
        with open(os.path.join(self.directory, "junk.json"), "w") as handle:
            handle.write("{not json")
        with open(os.path.join(self.directory, "notes.txt"), "w") as handle:
            handle.write("hello")
        self.assertEqual(registry.list_tunnels(), [])

    def test_find_takes_an_exact_id_or_an_unambiguous_prefix(self):
        process = self.spawn()
        registry.register(_record("ab120000", process.pid))
        registry.register(_record("ab340000", process.pid))

        self.assertEqual([r["id"] for r in registry.find("ab12")], ["ab120000"])
        self.assertEqual(len(registry.find("ab")), 2)
        self.assertEqual(registry.find("zz"), [])
        self.assertEqual(registry.find(""), [])

    def test_close_terminates_the_process_and_removes_its_files(self):
        process = self.spawn()
        registry.register(_record("cccc0000", process.pid, log=registry.log_path("cccc0000")))
        open(registry.log_path("cccc0000"), "w").close()

        self.assertTrue(registry.close(registry.find("cccc0000")[0]))

        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(os.path.exists(registry.record_path("cccc0000")))
        self.assertFalse(os.path.exists(registry.log_path("cccc0000")))

    def test_close_kills_a_tunnel_that_ignores_sigterm(self):
        process = self.spawn(ignore_term=True)
        registry.register(_record("stubborn", process.pid))

        self.assertTrue(registry.close(registry.find("stubborn")[0], grace_s=0.3))

        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        self.assertEqual(registry.list_tunnels(), [])

    def test_pid_alive_rejects_nonsense(self):
        for pid in (None, "x", 0, -1):
            self.assertFalse(registry.pid_alive(pid))
        self.assertFalse(registry.pid_alive(2 ** 22 + 12345))

    def test_log_tail_is_empty_without_a_log(self):
        self.assertEqual(registry.log_tail({"log": None}), [])
        self.assertEqual(registry.log_tail({"log": "/nonexistent/x.log"}), [])


class TunnelsCommandTests(RegistryTestCase):
    def test_json_list_is_one_object_with_every_running_tunnel(self):
        process = self.spawn()
        registry.register(_record("list0001", process.pid))

        ok, document = _run_json(tunnels_command.list_tunnels)

        self.assertTrue(ok)
        self.assertEqual([t["id"] for t in document["tunnels"]], ["list0001"])
        for key in ("pid", "instance", "token", "slot", "transport", "listen_host",
                    "listen_port", "gateway", "peer", "detached", "log", "started_at",
                    "age_secs"):
            self.assertIn(key, document["tunnels"][0])
        self.assertIn("read_at", document)

    def test_inspect_adds_the_log_tail(self):
        process = self.spawn()
        log = registry.log_path("insp0001")
        with open(log, "w") as handle:
            handle.write("Tunnel listening\n[127.0.0.1:5555] connected\n")
        registry.register(_record("insp0001", process.pid, detached=True, log=log))

        ok, document = _run_json(tunnels_command.list_tunnels, "insp")

        self.assertTrue(ok)
        self.assertEqual(document["tunnel"]["id"], "insp0001")
        self.assertEqual(document["tunnel"]["log_tail"][-1], "[127.0.0.1:5555] connected")

        text = io.StringIO()
        with redirect_stdout(text):
            tunnels_command.list_tunnels("insp0001")
        self.assertIn("slot 8080 of abcdef0123456789", text.getvalue())
        self.assertIn("(detached)", text.getvalue())

    def test_an_unknown_tunnel_is_an_error_object_and_a_failure(self):
        ok, document = _run_json(tunnels_command.list_tunnels, "nope")
        self.assertFalse(ok)
        self.assertIn("No running tunnel", document["error"])

    def test_an_ambiguous_prefix_names_the_candidates(self):
        process = self.spawn()
        registry.register(_record("dup10000", process.pid))
        registry.register(_record("dup20000", process.pid))

        ok, document = _run_json(tunnels_command.list_tunnels, "dup")

        self.assertFalse(ok)
        self.assertIn("dup10000", document["error"])
        self.assertIn("dup20000", document["error"])

    def test_the_empty_list_says_how_to_open_one(self):
        self.assertIn("--detach", tunnels_command.render_list([]))

    def test_the_table_fits_a_terminal(self):
        rows = [dict(_record("abcd1234", 1), age_secs=3700)]
        for line in tunnels_command.render_list(rows).splitlines():
            self.assertLess(len(line), 100, line)
        self.assertIn("1h", tunnels_command.render_list(rows))

    def test_close_by_id(self):
        process = self.spawn()
        registry.register(_record("shut0001", process.pid))

        ok, document = _run_json(tunnels_command.close_tunnels, ["shut0001"])

        self.assertTrue(ok)
        self.assertEqual(document["closed"], ["shut0001"])
        self.assertEqual(document["failed"], [])
        process.wait(timeout=5)

    def test_close_all(self):
        first, second = self.spawn(), self.spawn()
        registry.register(_record("all00001", first.pid))
        registry.register(_record("all00002", second.pid))

        ok, document = _run_json(tunnels_command.close_tunnels, [], close_all=True)

        self.assertTrue(ok)
        self.assertEqual(sorted(document["closed"]), ["all00001", "all00002"])
        self.assertEqual(registry.list_tunnels(), [])

    def test_close_of_an_unknown_tunnel_fails(self):
        ok, document = _run_json(tunnels_command.close_tunnels, ["ghost"])
        self.assertFalse(ok)
        self.assertIn("error", document)

    def test_close_without_arguments_prints_usage(self):
        ok, document = _run_json(tunnels_command.close_tunnels, [])
        self.assertFalse(ok)
        self.assertIn("Usage", document["error"])


class InstanceRelationshipTests(RegistryTestCase):
    """``nodo tunnels --instance``: the INSTANCES page's instance -> tunnels table."""

    def test_reaches_by_token_or_by_what_was_typed_but_never_through_a_peer(self):
        record = _record("rel00001", 1)
        self.assertTrue(registry.reaches(record, {"abcdef0123456789"}))
        self.assertTrue(registry.reaches(record, {"my-instance"}))
        self.assertFalse(registry.reaches(record, {"other", "", None}))
        remote = _record("rel00002", 1, peer="10.0.0.2:8090")
        self.assertFalse(registry.reaches(remote, {"abcdef0123456789"}))

    def test_for_instance_lists_only_the_tunnels_that_reach_it(self):
        process = self.spawn()
        registry.register(_record("mine0001", process.pid))
        registry.register(_record("other001", process.pid, instance="x", token="y"))

        self.assertEqual(
            [r["id"] for r in registry.for_instance({"abcdef0123456789"})], ["mine0001"]
        )

    def test_json_names_the_instance_and_its_tunnels(self):
        process = self.spawn()
        registry.register(_record("mine0002", process.pid))
        registry.register(_record("other002", process.pid, instance="x", token="y"))
        # The catalogue says `web` is the instance whose id the tunnel resolved to.
        with patch.object(tunnels_command, "instance_references",
                          return_value={"web", "abcdef0123456789"}):
            ok, document = _run_json(tunnels_command.list_tunnels, instance="web")

        self.assertTrue(ok)
        self.assertEqual(document["instance"], "web")
        self.assertEqual([t["id"] for t in document["tunnels"]], ["mine0002"])

    def test_an_instance_without_tunnels_says_how_to_open_one(self):
        text = io.StringIO()
        with patch.object(tunnels_command, "instance_references", return_value={"web"}), \
                redirect_stdout(text):
            self.assertTrue(tunnels_command.list_tunnels(instance="web"))
        self.assertIn("nodo tunnel web <slot> --detach", text.getvalue())

    def test_an_unreadable_catalogue_still_matches_the_reference_itself(self):
        with patch.dict(os.environ, {}, clear=False), \
                patch("sqlite3.connect", side_effect=sqlite3.Error("no db")):
            self.assertEqual(tunnels_command.instance_references("web"), {"web"})


class InboundSnapshotTests(RegistryTestCase):
    """``nodo tunnels --inbound``: what the daemon says it is relaying for others."""

    def _stream(self, **overrides):
        stream = {"id": "in000001", "caller": "ipv4:203.0.113.7:51000", "token": "abcdef0123456789",
                  "slot": 8080, "transport": "tcp", "target": "10.0.0.5:8080",
                  "started_at": int(time.time()) - 90, "bytes_in": 2048, "bytes_out": 3 << 20}
        stream.update(overrides)
        return stream

    def _write(self, pid, written_at, streams):
        with open(registry.inbound_path(), "w") as handle:
            json.dump({"pid": pid, "written_at": written_at, "streams": streams}, handle)

    def test_a_live_daemons_streams_are_read_with_their_age(self):
        registry.write_inbound([self._stream()])
        inbound = registry.read_inbound()
        self.assertEqual([s["id"] for s in inbound["streams"]], ["in000001"])
        self.assertGreaterEqual(inbound["streams"][0]["age_secs"], 90)
        self.assertIsNotNone(inbound["snapshot_at"])

    def test_a_snapshot_left_by_a_dead_daemon_lists_nothing(self):
        process = self.spawn()
        process.kill()
        process.wait()
        self._write(process.pid, int(time.time()), [self._stream()])
        self.assertEqual(registry.read_inbound(), {"streams": [], "snapshot_at": None})

    def test_a_stale_snapshot_lists_nothing(self):
        self._write(os.getpid(), int(time.time()) - registry.INBOUND_STALE_S - 5, [self._stream()])
        self.assertEqual(registry.read_inbound()["streams"], [])

    def test_no_snapshot_is_no_streams(self):
        self.assertEqual(registry.read_inbound(), {"streams": [], "snapshot_at": None})

    def test_the_snapshot_is_not_mistaken_for_a_tunnel_or_a_tunnel_id(self):
        registry.write_inbound([self._stream()])
        self.assertEqual(registry.list_tunnels(), [])
        self.assertEqual(completion.candidates("tunnels", {"storage": None}), [])

    def test_json_is_one_object(self):
        registry.write_inbound([self._stream()])
        ok, document = _run_json(tunnels_command.list_inbound)
        self.assertTrue(ok)
        self.assertEqual(document["inbound"][0]["caller"], "ipv4:203.0.113.7:51000")
        for key in ("id", "token", "slot", "transport", "target", "started_at",
                    "bytes_in", "bytes_out", "age_secs"):
            self.assertIn(key, document["inbound"][0])
        self.assertIn("snapshot_at", document)

    def test_the_table_says_who_reaches_what_and_how_much(self):
        text = tunnels_command.render_inbound([dict(self._stream(), age_secs=90)], 1)
        for expected in ("CALLER", "ipv4:203.0.113.7:51000", "8080", "2.0 KiB", "3.0 MiB", "1m"):
            self.assertIn(expected, text)
        for line in text.splitlines():
            self.assertLess(len(line), 100, line)
        self.assertIn("not relaying", tunnels_command.render_inbound([], 1))
        self.assertIn("not running", tunnels_command.render_inbound([], None))


@unittest.skipIf(kill_command is None, f"Missing runtime dependencies: {KILL_IMPORT_ERROR}")
class KillClosesTunnelsTests(RegistryTestCase):
    """``nodo kill`` closes the tunnels this host opened to the instance."""

    def setUp(self):
        super().setUp()
        self.patches = [
            patch.object(kill_command, "can_admin_network", return_value=True),
            # `web` is the name; the tunnel recorded the id it resolved to.
            patch.object(kill_command, "resolve_instance_token",
                         return_value="abcdef0123456789"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        super().tearDown()

    def test_kill_closes_the_instances_tunnels_and_leaves_the_others(self):
        mine, other = self.spawn(), self.spawn()
        registry.register(_record("mine0003", mine.pid, instance="abcdef0123456789"))
        registry.register(_record("othr0003", other.pid, instance="db", token="ffff"))

        with patch.object(kill_command, "stop_instance", return_value=0) as stop:
            ok, document = _run_json(kill_command.kill, "web")

        self.assertTrue(ok)
        # The command resolves the name; the manager takes only the token.
        stop.assert_called_once_with(token="abcdef0123456789")
        self.assertEqual(document["killed"], "abcdef0123456789")
        self.assertEqual(document["tunnels"], {"closed": ["mine0003"], "failed": []})
        self.assertEqual(mine.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual([r["id"] for r in registry.list_tunnels()], ["othr0003"])

    def test_the_text_output_names_each_closed_tunnel(self):
        process = self.spawn()
        registry.register(_record("mine0004", process.pid))
        text = io.StringIO()
        with patch.object(kill_command, "stop_instance", return_value=0), redirect_stdout(text):
            self.assertTrue(kill_command.kill("web"))
        self.assertIn("Service instance web deleted.", text.getvalue())
        self.assertIn("Closed tunnel mine0004", text.getvalue())

    def test_a_failed_kill_leaves_the_tunnels_alone(self):
        process = self.spawn()
        registry.register(_record("mine0005", process.pid))
        with patch.object(kill_command, "stop_instance", return_value=None):
            ok, document = _run_json(kill_command.kill, "web")
        self.assertFalse(ok)
        self.assertIn("error", document)
        self.assertEqual([r["id"] for r in registry.list_tunnels()], ["mine0005"])

    def test_without_the_capability_the_daemon_stops_it(self):
        """A CLI without root or CAP_NET_ADMIN cannot delete a tap; the daemon can."""
        process = self.spawn()
        registry.register(_record("mine0006", process.pid, instance="abcdef0123456789"))
        with patch.object(kill_command, "can_admin_network", return_value=False), patch.object(
            kill_command, "stop_instance", side_effect=AssertionError("stopped in-process")
        ), patch.object(kill_command, "stop_through_daemon", return_value=True) as daemon:
            ok, document = _run_json(kill_command.kill, "web")
        self.assertTrue(ok)
        daemon.assert_called_once_with("abcdef0123456789")
        self.assertEqual(document["tunnels"], {"closed": ["mine0006"], "failed": []})

    def test_without_the_capability_or_a_daemon_it_says_what_is_needed(self):
        with patch.object(kill_command, "can_admin_network", return_value=False), patch.object(
            kill_command, "stop_through_daemon", side_effect=ConnectionError("refused")
        ):
            ok, document = _run_json(kill_command.kill, "web")
        self.assertFalse(ok)
        self.assertIn("CAP_NET_ADMIN", document["error"])


class CompletionTests(RegistryTestCase):
    def test_tunnel_ids_complete_from_the_registry(self):
        process = self.spawn()
        registry.register(_record("comp0001", process.pid))
        self.assertEqual(completion.candidates("tunnels", {"storage": None}), ["comp0001"])

    def test_both_scripts_route_the_tunnel_commands_to_tunnel_ids(self):
        for script in (completion.bash_script("/n", "/p"), completion.zsh_script("/n", "/p")):
            self.assertIn('tunnels|tunnel_close) kind="tunnels"', script)


# Stands in for `nodo.py tunnel ...` under --detach: registers the way the real
# command does once bound, then serves (sleeps) until signalled.
_FAKE_NODO = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, {root!r})
    from src.utils import tunnel_registry as registry
    if "fail" in sys.argv:
        print("Error: cannot bind 127.0.0.1:1 -> Permission denied", flush=True)
        sys.exit(1)
    tunnel_id = os.environ[registry.ID_ENV]
    registry.register(registry.new_record(
        tunnel_id=tunnel_id, instance=sys.argv[2], token=sys.argv[2], slot=int(sys.argv[3]),
        udp=False, listen_host="127.0.0.1", listen_port=40000, gateway="127.0.0.1:8090",
        peer=None, detached=True, log=registry.log_path(tunnel_id), open_fee_mu=10000))
    print("Tunnel listening", flush=True)
    time.sleep(60)
""")


class PersistenceTests(RegistryTestCase):
    """Detached tunnels come back after a restart until closed on purpose."""

    def setUp(self):
        super().setUp()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.fake = os.path.join(self.directory, "fake_nodo.py")
        with open(self.fake, "w") as handle:
            handle.write(_FAKE_NODO.format(root=root))
        self.logged = []

    def tearDown(self):
        for record in registry.list_tunnels():
            registry.close(record)
        super().tearDown()

    def _restore(self, exists=lambda reference: True):
        return registry.restore(self.fake, exists, log=self.logged.append, timeout_s=20)

    def _spec(self, tunnel_id, argv=("my-instance", "8080"), **overrides):
        record = _record(tunnel_id, 1, listen_port=41000)
        registry.save_spec(record, list(argv))
        if overrides:
            with open(registry.spec_path(tunnel_id)) as handle:
                spec = json.load(handle)
            spec.update(overrides)
            with open(registry.spec_path(tunnel_id), "w") as handle:
                json.dump(spec, handle)

    def test_the_spec_is_pinned_to_the_port_the_tunnel_got(self):
        self._spec("pin00001")
        (spec,) = registry.list_specs()
        self.assertEqual(spec["argv"], ["my-instance", "8080", "--listen", "41000"])
        self._spec("pin00002", argv=("x", "53", "--listen", "5353"))
        self.assertEqual(registry.list_specs()[1]["argv"], ["x", "53", "--listen", "5353"])

    def test_the_spec_is_neither_a_tunnel_nor_a_tunnel_id(self):
        self._spec("pin00003")
        self.assertEqual(registry.list_tunnels(), [])
        self.assertEqual(completion.candidates("tunnels", {"storage": None}), [])

    def test_a_tunnel_gone_with_the_restart_is_reopened_under_its_id(self):
        self._spec("back0001")

        self.assertEqual(self._restore(), 1)

        (record,) = registry.list_tunnels()
        self.assertEqual(record["id"], "back0001")
        self.assertTrue(record["detached"])
        self.assertTrue(record["persistent"])
        self.assertIn("Reopened tunnel back0001", self.logged[0])

    def test_one_still_running_is_left_alone(self):
        process = self.spawn()
        registry.register(_record("live0002", process.pid))
        self._spec("live0002")
        self.assertEqual(self._restore(), 0)
        self.assertEqual([r["pid"] for r in registry.list_tunnels()], [process.pid])

    def test_one_whose_instance_is_gone_is_dropped_with_a_log_line(self):
        self._spec("gone0001")
        self.assertEqual(self._restore(exists=lambda reference: False), 0)
        self.assertEqual(registry.list_specs(), [])
        self.assertIn("no longer exists", self.logged[0])

    def test_a_peer_tunnel_is_not_checked_against_local_instances(self):
        self._spec("peer0001", peer="10.0.0.2:8090")
        self.assertEqual(self._restore(exists=lambda reference: False), 1)

    def test_one_that_fails_is_retried_then_dropped(self):
        self._spec("fail0001", argv=("fail", "8080"))
        for attempt in range(1, registry.RESTORE_ATTEMPTS):
            self.assertEqual(self._restore(), 0)
            (spec,) = registry.list_specs()
            self.assertEqual(spec["failures"], attempt)
            self.assertIn("will retry", self.logged[-1])
        self._restore()
        self.assertEqual(registry.list_specs(), [])
        self.assertIn("Dropped tunnel fail0001", self.logged[-1])
        self.assertIn("cannot bind", self.logged[-1])

    def test_closing_on_purpose_forgets_it(self):
        self._spec("shut0002")
        self._restore()
        self.assertTrue(registry.close(registry.find("shut0002")[0]))
        self.assertEqual(registry.list_specs(), [])
        self.assertEqual(self._restore(), 0, "not reopened again")

    def test_tunnel_close_and_kill_go_through_the_same_close(self):
        self._spec("shut0003")
        self._restore()
        ok, document = _run_json(tunnels_command.close_tunnels, ["shut0003"])
        self.assertTrue(ok)
        self.assertEqual(registry.list_specs(), [])


@unittest.skipIf(tunnel_command is None, f"Missing runtime dependencies: {TUNNEL_IMPORT_ERROR}")
class FeeTests(unittest.TestCase):
    """Opening is never prompted on the CLI (agents run it); the fee is in the output."""

    def test_the_record_carries_the_open_fee(self):
        record = registry.new_record(
            tunnel_id="fee00001", instance="i", token="t", slot=80, udp=False,
            listen_host="127.0.0.1", listen_port=1, gateway="g", peer=None,
            detached=False, open_fee_mu=10000,
        )
        self.assertEqual(record["open_fee_mu"], 10000)

    def test_the_fee_is_this_nodes_price_except_through_a_peer(self):
        with patch("src.utils.monetary.prices") as prices:
            prices.return_value.tunnel_open_mu = 10000
            self.assertEqual(tunnel_command.open_fee_mu(None), 10000)
            self.assertIsNone(tunnel_command.open_fee_mu("10.0.0.2:8090"))

    def test_the_fee_line_says_what_a_connection_costs(self):
        with patch("src.utils.monetary.format_mu", return_value="10,000 MU"):
            line = tunnel_command.describe_fee({"open_fee_mu": 10000})
        self.assertIn("each connection spends 10,000 MU", line)
        self.assertIn("TUNNEL_OPEN_MU", line)
        self.assertIn("free", tunnel_command.describe_fee({"open_fee_mu": 0}))
        self.assertIn("its own prices", tunnel_command.describe_fee({"open_fee_mu": None}))


@unittest.skipIf(tunnel_command is None, f"Missing runtime dependencies: {TUNNEL_IMPORT_ERROR}")
class DetachTests(RegistryTestCase):
    def setUp(self):
        super().setUp()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.fake = os.path.join(self.directory, "fake_nodo.py")
        with open(self.fake, "w") as handle:
            handle.write(_FAKE_NODO.format(root=root))
        self.nodo_py = patch.object(tunnel_command, "NODO_PY", self.fake)
        self.nodo_py.start()

    def tearDown(self):
        self.nodo_py.stop()
        for record in registry.list_tunnels():
            registry.close(record)
        super().tearDown()

    def test_detach_returns_the_registered_tunnel_once_it_is_up(self):
        ok, document = _run_json(tunnel_command.detach, ["my-instance", "8080"], timeout_s=20)

        self.assertTrue(ok, document)
        record = document["tunnel"]
        self.assertEqual(record["slot"], 8080)
        self.assertTrue(record["detached"])
        self.assertEqual(record["open_fee_mu"], 10000, "the fee is in the --json output")
        self.assertEqual([r["id"] for r in registry.list_tunnels()], [record["id"]])
        self.assertTrue(registry.pid_alive(record["pid"]))
        # Kept, pinned to its port, for the daemon to reopen after a restart.
        self.assertTrue(record["persistent"])
        (spec,) = registry.list_specs()
        self.assertEqual(spec["argv"], ["my-instance", "8080", "--listen", "40000"])

    def test_detach_reports_why_the_tunnel_could_not_start(self):
        ok, document = _run_json(tunnel_command.detach, ["fail", "8080"], timeout_s=20)

        self.assertFalse(ok)
        self.assertIn("cannot bind", document["error"])
        self.assertEqual(os.listdir(self.directory), ["fake_nodo.py"], "no files left behind")
        self.assertEqual(registry.list_specs(), [], "nothing to reopen")


if __name__ == "__main__":
    unittest.main()
