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
        ), mock.patch(
            "src.utils.java_dependency.ensure_java_runtime",
            side_effect=JavaDependencyMissing("no java"),
        ):
            alerts = operator_alerts.collect()

        self.assertEqual([alert.key for alert in alerts], ["gateway_port_firewall", "java_missing"])

    def test_a_healthy_node_collects_nothing(self):
        with mock.patch.object(operator_alerts, "gateway_port_alert", return_value=None), \
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
        self.assertIn("collect_alerts()", info_arm)
        self.assertIn("as_line()", info_arm)


if __name__ == "__main__":
    unittest.main()
