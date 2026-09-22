"""The two failures that leave a node looking healthy while it cannot do its job.

Both were detected already, and both were announced only to ``storage/app.log``
and to the tail of a ``nodo serve`` nobody is watching:

* the gateway port needing to be opened in the host firewall, and
* Java being absent, which takes payments and reputation with it in complete
  silence -- ``registry.contracts()`` drops a contract that cannot settle, and a
  node advertising no payment method looks exactly like one configured without
  any.

``src/utils/operator_alerts.py`` is where they are asked about, so ``nodo info``
and the TUI can say the same thing in the same words. These tests pin the three
properties that make that worth anything: the alert appears when the condition
holds, it *disappears* when it is fixed, and asking costs nothing (no subprocess,
no socket, no write).
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import operator_alerts
from src.utils.operator_alerts import ACTION_REQUIRED


class _FakeConfigManager:
    """Just enough ConfigManager for the gateway alert: a port and a config path.

    A stub rather than a real one because constructing a `ConfigManager` loads (and
    may rewrite) a config file, and this test is about what is *read*.
    """

    def __init__(self, config_path, port):
        self.config_path = config_path
        self._port = port

    def gateway_port_or_none(self):
        return self._port


class GatewayPortAlertTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config_path = os.path.join(self._dir.name, "config.yaml")
        with open(self.config_path, "w") as handle:
            handle.write("main: {}\n")

    def _notice_path(self):
        from src.utils.config import GATEWAY_NOTICE_FILE

        return os.path.join(self._dir.name, GATEWAY_NOTICE_FILE)

    def _write_notice(self, text="open TCP 52285 with firewall-cmd --add-port=52285/tcp"):
        with open(self._notice_path(), "w") as handle:
            handle.write(text)

    def test_an_assigned_port_with_nothing_pending_is_not_an_alert(self):
        """The ordinary state of a working node produces no line at all.

        The property that makes the banner worth looking at: an alert that is
        always on screen is decoration.
        """
        manager = _FakeConfigManager(self.config_path, 52285)

        self.assertIsNone(operator_alerts.gateway_port_alert(manager))
        # Nor on a node that is up, which is what "working" means here.
        self.assertIsNone(operator_alerts.gateway_port_alert(manager, serving=True))

    def test_a_settled_port_nothing_is_listening_on_is_its_own_alert(self):
        """Everything the operator would check is correct, and the node is still down.

        The port is assigned, there is no pending firewall question, and nothing
        answers on it. Neither of the other two alerts can say that -- one is about
        a port that was never assigned, the other about a `.gateway_notice` that is
        not on disk -- so without this the node is silently earning nothing while
        every configuration an operator would inspect reads as fine.
        """
        manager = _FakeConfigManager(self.config_path, 52285)

        alert = operator_alerts.gateway_port_alert(manager, serving=False)

        self.assertIsNotNone(alert)
        self.assertEqual(alert.key, "gateway_port_closed")
        # Consequence first, as everywhere else on this banner.
        self.assertTrue(alert.summary.startswith("NOT SERVING -"), alert.summary)
        self.assertIn("52285", alert.summary)
        self.assertIn("earns nothing", alert.summary)
        self.assertIn("sudo nodo serve", alert.summary)

    def test_an_unknown_serving_state_does_not_claim_the_port_is_dead(self):
        """`None` is "not known here", which is not the same as "nothing answers".

        It is the state before the TUI's first `nodo info` returns. Raising the
        alert there would put a red banner on the first frame of every run.
        """
        manager = _FakeConfigManager(self.config_path, 52285)

        self.assertIsNone(operator_alerts.gateway_port_alert(manager, serving=None))

    def test_a_pending_firewall_notice_is_reported_instead_of_the_bare_silence(self):
        """One cause, one alert, and the more specific diagnosis wins.

        Both describe the same silence on the same port. The firewall one names the
        command that fixes it; reporting the other alongside would be two lines
        about one problem, one of which sends the operator to the wrong fix.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)

        alert = operator_alerts.gateway_port_alert(manager, serving=False)

        self.assertEqual(alert.key, "gateway_port_firewall")

    def test_a_pending_notice_beside_an_assigned_port_is_an_alert(self):
        """`.gateway_notice` on disk means the last look at this port ended badly.

        The file is written by `ConfigManager._gateway_notice_unlocked` and by
        `serve.py`'s refusal to start, and is removed the moment the port is proven
        reachable -- so its presence *is* "there is an open question here", with no
        second lifetime to keep in step.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)

        alert = operator_alerts.gateway_port_alert(manager)

        self.assertIsNotNone(alert)
        self.assertEqual(alert.key, "gateway_port_firewall")
        # The port is named. "Open the gateway port" with no number is an
        # instruction the operator cannot carry out without going and looking.
        self.assertIn("52285", alert.summary)
        # And the full instructions travel with it, for the places with room.
        self.assertIn("firewall-cmd", alert.detail)

    def test_the_firewall_alert_leads_with_the_consequence(self):
        """What is wrong first, why second.

        A line that opens with the mechanism is a line the operator has to finish
        reading before learning that their node is not serving anybody -- and in
        `nodo info` it sits among a dozen ordinary `key: value` lines.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)

        for serving, lead in (
            (False, "NOT SERVING -"),
            (True, "RUNNING BUT UNREACHABLE -"),
            (None, "NOT REACHABLE FROM OUTSIDE -"),
        ):
            alert = operator_alerts.gateway_port_alert(manager, serving)

            self.assertTrue(alert.summary.startswith(lead), alert.summary)
            self.assertIn("peers cannot reach this node", alert.summary)

    def test_a_running_node_and_a_stopped_one_do_not_get_the_same_sentence(self):
        """Up-and-unreachable is the state nothing inside the host can see.

        Every local check answers, the node looks healthy from the machine it runs
        on, and it is earning nothing the entire time. It is worth its own words.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)

        up = operator_alerts.gateway_port_alert(manager, serving=True)
        down = operator_alerts.gateway_port_alert(manager, serving=False)

        self.assertNotEqual(up.summary, down.summary)
        self.assertEqual(up.key, down.key)

    def test_the_alert_clears_when_the_notice_is_removed(self):
        """Proving the port reachable deletes the file; the alert has to go with it.

        This is the whole reason the alert is *derived* rather than recorded: there
        is no acknowledge and no dismiss, so it cannot be silenced by anything
        except the condition actually being fixed.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)
        self.assertIsNotNone(operator_alerts.gateway_port_alert(manager))

        os.unlink(self._notice_path())

        self.assertIsNone(operator_alerts.gateway_port_alert(manager))

    def test_an_unassigned_port_is_its_own_alert_with_its_own_fix(self):
        """`auto` is a different problem from "opened but unreachable".

        Nothing has been opened, nothing has been stored, and the fix is one
        privileged start rather than a firewall command -- so the two must not share
        a message.
        """
        manager = _FakeConfigManager(self.config_path, None)

        alert = operator_alerts.gateway_port_alert(manager)

        self.assertIsNotNone(alert)
        self.assertEqual(alert.key, "gateway_port_unassigned")
        self.assertTrue(alert.summary.startswith("NOT SERVING -"), alert.summary)
        self.assertIn("sudo nodo serve", alert.summary)

    def test_an_empty_notice_file_is_not_an_alert(self):
        """A zero-byte notice carries no instructions.

        An interrupted write or a full disk leaves one behind, and an alert whose
        body is a blank line tells the operator less than no alert at all.
        """
        with open(self._notice_path(), "w"):
            pass
        manager = _FakeConfigManager(self.config_path, 52285)

        self.assertIsNone(operator_alerts.gateway_port_alert(manager))

    def test_a_config_that_cannot_be_read_produces_no_alert(self):
        """A broken config is a bigger problem, and not this one.

        Reporting "the port is unassigned" because YAML failed to parse would send
        the operator to fix the wrong thing.
        """

        class Exploding:
            config_path = self.config_path

            def gateway_port_or_none(self):
                raise RuntimeError("config.yaml is not valid YAML")

        self.assertIsNone(operator_alerts.gateway_port_alert(Exploding()))

    def test_asking_never_spawns_a_process_or_opens_a_socket(self):
        """`nodo info` runs this on every invocation and the TUI on every data tick.

        Proving the port reachable rebuilds a network namespace; that is the
        daemon's job, once per boot. This reports the *stored* verdict, which is
        what makes it cheap enough to ask this often.
        """
        self._write_notice()
        manager = _FakeConfigManager(self.config_path, 52285)

        with mock.patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("spawned a process")), \
             mock.patch("socket.socket", side_effect=AssertionError("opened a socket")):
            alert = operator_alerts.gateway_port_alert(manager)

        self.assertIsNotNone(alert)


class _FakePlaintextConfigManager:
    """Just enough ConfigManager for the plaintext gateway alert."""

    def __init__(self, config_path, port):
        self.config_path = config_path
        self._port = port

    def get_plaintext_gateway_port(self):
        return self._port


class PlaintextGatewayPortAlertTests(unittest.TestCase):
    """The guest-only counterpart of GatewayPortAlertTests.

    Different audience (microVMs, not peers), different trigger (a notice file of
    its own, ``.gateway_plaintext_notice``), and one state the TLS port does not
    have: turned off (``0``) is not a failure, so it must raise nothing at all.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config_path = os.path.join(self._dir.name, "config.yaml")
        with open(self.config_path, "w") as handle:
            handle.write("main: {}\n")

    def _notice_path(self):
        from src.utils.config import GATEWAY_PLAINTEXT_NOTICE_FILE

        return os.path.join(self._dir.name, GATEWAY_PLAINTEXT_NOTICE_FILE)

    def _write_notice(self, text="open TCP 52286, scoped to 192.168.200.0/24"):
        with open(self._notice_path(), "w") as handle:
            handle.write(text)

    def test_a_reachable_port_with_nothing_pending_is_not_an_alert(self):
        manager = _FakePlaintextConfigManager(self.config_path, 52286)

        self.assertIsNone(operator_alerts.plaintext_gateway_port_alert(manager))

    def test_a_pending_notice_names_the_port_and_talks_about_microvms(self):
        self._write_notice()
        manager = _FakePlaintextConfigManager(self.config_path, 52286)

        alert = operator_alerts.plaintext_gateway_port_alert(manager)

        self.assertIsNotNone(alert)
        self.assertEqual(alert.key, "gateway_plaintext_port_unreachable")
        self.assertIn("52286", alert.summary)
        # Never "peers": this port is not announced to them at all.
        self.assertNotIn("peer", alert.summary.lower())
        self.assertIn("scoped to 192.168.200.0/24", alert.detail)

    def test_the_port_turned_off_raises_nothing_even_with_a_stray_notice(self):
        """0 is the operator's own choice (services fall back to the TLS port).

        A leftover notice from before it was disabled must not haunt the banner
        forever -- there is no `serving` state here to distinguish, unlike the TLS
        port, and reporting a port nobody is checking any more is worse than
        silence.
        """
        self._write_notice()
        manager = _FakePlaintextConfigManager(self.config_path, 0)

        self.assertIsNone(operator_alerts.plaintext_gateway_port_alert(manager))

    def test_the_alert_clears_when_the_notice_is_removed(self):
        self._write_notice()
        manager = _FakePlaintextConfigManager(self.config_path, 52286)
        self.assertIsNotNone(operator_alerts.plaintext_gateway_port_alert(manager))

        os.unlink(self._notice_path())

        self.assertIsNone(operator_alerts.plaintext_gateway_port_alert(manager))

    def test_an_empty_notice_file_is_not_an_alert(self):
        with open(self._notice_path(), "w"):
            pass
        manager = _FakePlaintextConfigManager(self.config_path, 52286)

        self.assertIsNone(operator_alerts.plaintext_gateway_port_alert(manager))

    def test_a_config_that_cannot_be_read_produces_no_alert(self):
        class Exploding:
            config_path = self.config_path

            def get_plaintext_gateway_port(self):
                raise RuntimeError("config.yaml is not valid YAML")

        self.assertIsNone(operator_alerts.plaintext_gateway_port_alert(Exploding()))

    def test_asking_never_spawns_a_process_or_opens_a_socket(self):
        self._write_notice()
        manager = _FakePlaintextConfigManager(self.config_path, 52286)

        with mock.patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("spawned a process")), \
             mock.patch("socket.socket", side_effect=AssertionError("opened a socket")):
            alert = operator_alerts.plaintext_gateway_port_alert(manager)

        self.assertIsNotNone(alert)


class JavaAlertTests(unittest.TestCase):
    def test_a_missing_runtime_is_an_alert_that_names_the_install_command(self):
        """The operator gets the command, not the diagnosis.

        "Java is not installed" leaves them searching; the bundled installer script
        is the answer and it is one line.
        """
        from src.utils.java_dependency import JavaDependencyMissing

        with mock.patch(
            "src.utils.java_dependency.ensure_java_runtime",
            side_effect=JavaDependencyMissing("no java"),
        ):
            alert = operator_alerts.java_alert()

        self.assertIsNotNone(alert)
        self.assertEqual(alert.key, "java_missing")
        self.assertIn("install_java.sh", alert.summary)
        # And why it matters, where there is room: this failure is silent, which is
        # the only reason it needs announcing at all.
        self.assertIn("no payment method", alert.detail)

    def test_an_installed_runtime_produces_nothing(self):
        with mock.patch("src.utils.java_dependency.ensure_java_runtime", return_value=None):
            self.assertIsNone(operator_alerts.java_alert())
            self.assertTrue(operator_alerts.java_is_available())

    def test_the_check_reuses_ensure_java_runtime_rather_than_restating_it(self):
        """One definition of "is Java here", not two.

        `ensure_java_runtime` looks at JAVA_HOME, then the configured path, then
        PATH, and the order is load-bearing (a stale configured path used to end the
        search and take the node's whole payment system with it). A second copy of
        those rules here would drift, and the symptom would be `nodo info` reporting
        Java as present on a node that refuses to pay.
        """
        with mock.patch(
            "src.utils.java_dependency.ensure_java_runtime", return_value=None
        ) as ensure:
            operator_alerts.java_alert()

        ensure.assert_called_once()

    def test_a_failure_that_is_not_about_java_is_not_reported_as_missing_java(self):
        with mock.patch(
            "src.utils.java_dependency.ensure_java_runtime",
            side_effect=RuntimeError("the config file vanished"),
        ):
            self.assertIsNone(operator_alerts.java_alert())


class CollectTests(unittest.TestCase):
    def test_the_gateway_port_is_reported_before_java(self):
        """Order is not cosmetic: one of these stops the node entirely.

        A node that cannot serve has no use for a payment system, so the port is
        the thing to fix first and therefore the line to read first.
        """
        from src.utils.java_dependency import JavaDependencyMissing

        with mock.patch.object(
            operator_alerts,
            "gateway_port_alert",
            return_value=operator_alerts.OperatorAlert("gateway_port_firewall", "port"),
        ), mock.patch.object(
            operator_alerts, "plaintext_gateway_port_alert", return_value=None
        ), mock.patch(
            "src.utils.java_dependency.ensure_java_runtime",
            side_effect=JavaDependencyMissing("no java"),
        ):
            alerts = operator_alerts.collect()

        self.assertEqual([alert.key for alert in alerts], ["gateway_port_firewall", "java_missing"])

    def test_the_plaintext_alert_sits_between_the_tls_port_and_java(self):
        """Stops fewer things than the TLS port, more things than Java missing.

        The TLS alert means the node is not serving anybody; the plaintext one
        means the node is serving peers fine but every microVM it launches is cut
        off from calling back into it. Reading order should say so.
        """
        from src.utils.java_dependency import JavaDependencyMissing

        with mock.patch.object(operator_alerts, "gateway_port_alert", return_value=None), \
             mock.patch.object(
                 operator_alerts,
                 "plaintext_gateway_port_alert",
                 return_value=operator_alerts.OperatorAlert(
                     "gateway_plaintext_port_unreachable", "plaintext"
                 ),
             ), mock.patch(
                 "src.utils.java_dependency.ensure_java_runtime",
                 side_effect=JavaDependencyMissing("no java"),
             ):
            alerts = operator_alerts.collect()

        self.assertEqual(
            [alert.key for alert in alerts],
            ["gateway_plaintext_port_unreachable", "java_missing"],
        )

    def test_a_healthy_node_collects_nothing(self):
        with mock.patch.object(operator_alerts, "gateway_port_alert", return_value=None), \
             mock.patch.object(operator_alerts, "plaintext_gateway_port_alert", return_value=None), \
             mock.patch("src.utils.java_dependency.ensure_java_runtime", return_value=None):
            self.assertEqual(operator_alerts.collect(), [])

    def test_the_printed_line_is_marked_so_it_survives_a_wall_of_output(self):
        """`nodo info` is a dozen `key: value` lines.

        An alert phrased like the rest of them reads as one more fact about the
        node rather than as something to act on, which is how it ended up being
        missed in app.log in the first place.
        """
        alert = operator_alerts.OperatorAlert("k", "Open TCP 52285.")

        self.assertEqual(alert.as_line(), f"{ACTION_REQUIRED} Open TCP 52285.")


class NodoInfoWiringTests(unittest.TestCase):
    """`nodo info` actually asks.

    Read off the source rather than by running the command, which starts a JVM,
    reads the chain and calls `os._exit`. The point is only that the call is still
    in the `info` arm at all -- a check that exists and is never invoked is the
    state this whole change is fixing.
    """

    def test_info_collects_operator_alerts(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "nodo.py"), "r") as handle:
            source = handle.read()

        info_arm = source.split('case "info":', 1)[1].split('case "logs":', 1)[0]

        self.assertIn("operator_alerts", info_arm)
        self.assertIn("collect_alerts(serving=serving)", info_arm)
        self.assertIn("as_line()", info_arm)

    def test_info_reuses_the_serving_check_it_already_made(self):
        """The alert says whether the node is down or up-and-unreachable.

        `nodo info` prints that status on its first line, so the answer is already
        in hand: asking `is_serving()` a second time would put another socket
        connect on a command whose alert block is meant to cost two `stat` calls.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "nodo.py"), "r") as handle:
            source = handle.read()

        info_arm = source.split('case "info":', 1)[1].split('case "logs":', 1)[0]

        self.assertEqual(info_arm.count("is_serving()"), 1)
        self.assertLess(
            info_arm.index("serving = is_serving()"),
            info_arm.index("collect_alerts(serving=serving)"),
        )


if __name__ == "__main__":
    unittest.main()
