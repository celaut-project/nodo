"""Assigning the gateway port, and remembering that it was proven.

The bug this started from: the port was resolved from ``auto`` to a free port,
opened in the firewall *only if the process happened to be root*, and persisted
either way. So a single unprivileged ``nodo <anything>`` consumed the sentinel, and
every later daemon start read a plausible port and never opened anything.

The fix after that one overcorrected. Assignment was made to prove the port before
storing it, which cannot be done before the guest bridge exists -- so on a host
with firewalld and no bridge yet, nothing was ever stored and the operator's only
way forward was to pin a port by hand, unverified. The port is therefore stored
once it is *opened*, and reachability is proven later, in the start path, where the
bridge exists and where a negative answer can stop the node. What the operator gets
in exchange is the stability the old candidate cache existed to provide: the port
in the instructions is still the port tomorrow.

The verdict itself is cached in ``<CACHE>/gateway_port_passed`` so the daemon does
not rebuild a network namespace on every restart -- keyed to the boot, because the
netfilter state it describes does not outlive one either.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.config import (
    GATEWAY_NOTICE_FILE,
    GATEWAY_PORT_PASSED_FILE,
    ConfigManager,
)
from src.utils.firewall.gateway import GatewayPortUnavailable
from src.utils.singleton import Singleton


class _ManagerCase(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _manager(self, tmpdir, gateway_port="auto", with_cache=True, log=None):
        cache = (
            "main:\n"
            f"  STORAGE: {Path(tmpdir) / 'storage'}\n"
            "  CACHE: ${main.STORAGE}/__cache__/\n"
        ) if with_cache else ""
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"{cache}network:\n"
            f"  GATEWAY_PORT: {gateway_port}\n"
            "  FREE_PORTS_RANGE:\n    - START: 41000\n      END: 41100\n",
            encoding="utf-8",
        )
        manager = ConfigManager(
            config_path=str(config_path), **({"log": log} if log else {})
        )
        return manager, config_path

    @staticmethod
    def _marker(tmpdir):
        return Path(tmpdir) / "storage" / "__cache__" / GATEWAY_PORT_PASSED_FILE


class GatewayPortResolutionTests(_ManagerCase):
    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41000)
    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_unprivileged_run_leaves_the_sentinel_alone(
        self, _euid, mock_free_port, mock_assign
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

            self.assertIsNone(manager.gateway_port_or_none())
            # Neither picked nor opened, and nothing written back.
            mock_free_port.assert_not_called()
            mock_assign.assert_not_called()
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41001)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_privileged_run_opens_then_persists(self, _euid, _free_port, mock_assign):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            self.assertEqual(manager.assign_gateway_port_if_unset(), 41001)

            self.assertEqual(manager.get_gateway_port(), 41001)
            self.assertIn("GATEWAY_PORT: 41001", config_path.read_text(encoding="utf-8"))
            self.assertEqual(mock_assign.call_args.kwargs["port"], 41001)

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41003)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_assignment_is_not_asked_to_probe_anything(
        self, _euid, _free_port, mock_assign
    ):
        # No bridge, no gateway IP, no subnet: there is nothing to probe from at
        # config-load time, and pretending otherwise is what made a firewalld host
        # unassignable. The start path owns that question now.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

        for absent in ("bridge", "gateway_ip", "subnet"):
            self.assertNotIn(absent, mock_assign.call_args.kwargs)

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("no backend", "install nftables", port=41002),
    )
    @patch("src.utils.config.get_free_port", return_value=41002)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_a_port_that_could_not_be_opened_is_not_persisted(
        self, _euid, _free_port, _assign
    ):
        # The one refusal assignment still owns: no rule was applied, so there is
        # nothing to store. The sentinel survives and the next start tries again.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

            self.assertIsNone(manager.gateway_port_or_none())
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))

    @patch("src.utils.firewall.gateway.withdraw_gateway_port")
    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41004)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_a_port_that_could_not_be_saved_has_its_rule_taken_back_out(
        self, _euid, _free_port, _assign, mock_withdraw
    ):
        # The port was opened for real and then never written down. Nothing would
        # ever clean that rule up: the pruning that removes stale gateway rules keys
        # off the port in config.yaml, which here is still `auto`.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.load_config()
            with patch.object(
                ConfigManager, "_save_config_unlocked", side_effect=OSError("read-only")
            ):
                with self.assertRaises(OSError):
                    manager.assign_gateway_port_if_unset()

        self.assertEqual(mock_withdraw.call_args.args[0], 41004)

    @patch("src.utils.firewall.gateway.withdraw_gateway_port")
    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41005)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_a_saved_port_keeps_its_rule(
        self, _euid, _free_port, _assign, mock_withdraw
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

        mock_withdraw.assert_not_called()

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port")
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_an_already_assigned_port_is_never_reassigned(
        self, _euid, mock_free_port, mock_assign
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, gateway_port="58443")
            self.assertEqual(manager.assign_gateway_port_if_unset(), 58443)

            mock_free_port.assert_not_called()
            mock_assign.assert_not_called()


    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.get_free_port", return_value=41006)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_merely_loading_the_config_assigns_nothing(
        self, _euid, mock_free_port, mock_assign
    ):
        # Assignment writes a firewall rule and a config value. It used to happen on
        # load, so any privileged `nodo <anything>` -- down to the completion helper
        # the shell runs on a Tab keypress -- did both.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            mock_free_port.assert_not_called()
            mock_assign.assert_not_called()
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))


class GatewayPortAccessorTests(_ManagerCase):
    """``auto`` must never reach a caller: no port is an exception, not a value."""

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_get_raises_instead_of_returning_the_sentinel(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "auto")
            for key in ("GATEWAY_PORT", "network.GATEWAY_PORT"):
                with self.subTest(key=key):
                    with self.assertRaises(GatewayPortUnavailable) as ctx:
                        manager.get(key)
                    # The message has to tell an operator what to do next.
                    self.assertIn("sudo nodo serve", ctx.exception.instructions)

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_or_none_reports_the_absence_without_raising(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "auto")
            self.assertIsNone(manager.gateway_port_or_none())

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_assigned_port_comes_back_as_an_int(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            self.assertEqual(manager.get("GATEWAY_PORT"), 58443)
            self.assertEqual(manager.get("network.GATEWAY_PORT"), 58443)
            self.assertEqual(manager.get_gateway_port(), 58443)

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_an_unusable_value_is_treated_as_unassigned(self, _euid):
        for bad in ('"not-a-port"', "0", "70000", '""'):
            with self.subTest(value=bad):
                Singleton._instances.pop(ConfigManager, None)
                with tempfile.TemporaryDirectory() as tmpdir:
                    manager, _ = self._manager(tmpdir, bad)
                    self.assertIsNone(manager.gateway_port_or_none())
                    with self.assertRaises(GatewayPortUnavailable):
                        manager.get_gateway_port()


@patch("src.utils.config.os.geteuid", return_value=1000)
class VerdictCacheTests(_ManagerCase):
    """``gateway_port_passed``: proven once, not re-proven on every restart.

    The probe builds a network namespace and a veth pair, which is not something to
    redo on every start of a node whose answer has not changed. What makes skipping
    it safe is what invalidates the file: a different port, a different boot, or any
    write to ``network.GATEWAY_PORT``.
    """

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_a_marked_port_reads_back_as_passed(self, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)

            self.assertTrue(manager.gateway_port_passed(58443))
            self.assertTrue(self._marker(tmpdir).exists())

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_no_marker_means_not_passed(self, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            self.assertFalse(manager.gateway_port_passed(58443))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_a_verdict_about_another_port_does_not_count(self, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)
            self.assertFalse(manager.gateway_port_passed(41000))

    def test_a_verdict_from_another_boot_does_not_count(self, _euid):
        # An operator who opened the port with no --permanent lost the rule on the
        # reboot. A marker that outlived it would skip the one check that catches
        # that, so the verdict dies with the netfilter state it was about.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            with patch(
                "src.utils.config.ConfigManager._boot_id", return_value="boot-a"
            ):
                manager.mark_gateway_port_passed(58443)
            with patch(
                "src.utils.config.ConfigManager._boot_id", return_value="boot-b"
            ):
                self.assertFalse(manager.gateway_port_passed(58443))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="")
    def test_without_a_boot_id_nothing_is_ever_skipped(self, _boot, _euid):
        # "I cannot tell which boot this was" is not a reason to trust the file.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)
            self.assertFalse(manager.gateway_port_passed(58443))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_writing_the_port_throws_the_verdict_away(self, _boot, _euid):
        # The path an operator actually takes: pin a port by hand. A verdict about
        # the old one says nothing about this one.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)

            manager.set("network.GATEWAY_PORT", 52285)

            self.assertFalse(self._marker(tmpdir).exists())
            self.assertFalse(manager.gateway_port_passed(52285))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_rewriting_the_same_port_keeps_the_verdict(self, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)

            manager.set("network.GATEWAY_PORT", 58443)

            self.assertTrue(manager.gateway_port_passed(58443))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_another_key_leaves_the_verdict_alone(self, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)

            manager.set("network.VERIFY_GATEWAY_REACHABILITY", False)

            self.assertTrue(manager.gateway_port_passed(58443))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    @patch("src.utils.firewall.gateway.assign_gateway_port")
    def test_a_newly_assigned_port_starts_unproven(self, _assign, _boot, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443")
            manager.mark_gateway_port_passed(58443)

            # A fresh assignment on top of a stale marker, as a reinstall would do.
            with patch("src.utils.config.os.geteuid", return_value=0):
                manager.set("network.GATEWAY_PORT", "auto")
                Singleton._instances.pop(ConfigManager, None)
                manager, _ = self._manager(tmpdir)
                manager.assign_gateway_port_if_unset()

            self.assertFalse(self._marker(tmpdir).exists())

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    def test_an_unusable_cache_path_still_gets_a_marker(self, _boot, _euid):
        # No main.CACHE to resolve: it lands beside config.yaml instead. Having the
        # file somewhere is what matters, not where.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, "58443", with_cache=False)
            manager.mark_gateway_port_passed(58443)

            self.assertTrue((Path(tmpdir) / GATEWAY_PORT_PASSED_FILE).exists())
            self.assertTrue(manager.gateway_port_passed(58443))


class GatewayNoticeTests(_ManagerCase):
    """The alert: framed, held back to the end, and left on disk for the installer."""

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_the_log_gets_a_pointer_rather_than_a_second_copy(self, _euid):
        # The fallback logger prints to stderr, so logging the framed block would put
        # the alert in the middle of the output as well as at the end.
        with tempfile.TemporaryDirectory() as tmpdir:
            logged = []
            manager, _ = self._manager(tmpdir, log=logged.append)
            manager.assign_gateway_port_if_unset()

        message = next(m for m in logged if "not assigned" in m)
        self.assertNotIn("--------", message)
        self.assertIn("end of this run", message)

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_the_unprivileged_notice_is_a_separated_block(self, _euid):
        from src.utils.firewall import gateway

        gateway._DEFERRED_NOTICES.clear()
        self.addCleanup(gateway._DEFERRED_NOTICES.clear)
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

        message = next(m for m in gateway._DEFERRED_NOTICES if "not root" in m)
        self.assertTrue(message.startswith("\n"), repr(message[:40]))
        self.assertTrue(message.endswith("\n"), repr(message[-40:]))

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_it_is_deferred_rather_than_printed_in_place(self, _euid):
        from src.utils.firewall import gateway

        gateway._DEFERRED_NOTICES.clear()
        self.addCleanup(gateway._DEFERRED_NOTICES.clear)
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            with patch("builtins.print") as mock_print:
                manager.assign_gateway_port_if_unset()

            # Whatever lands in the middle of the output is the pointer, never the
            # framed alert: that one waits for the end.
            for call in mock_print.call_args_list:
                self.assertNotIn("-----", call.args[0])
        self.assertTrue(
            any("not root" in notice for notice in gateway._DEFERRED_NOTICES),
            gateway._DEFERRED_NOTICES,
        )

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_it_is_left_beside_config_yaml_for_the_installer(self, _euid):
        # install.sh prints this file as its very last act: the notice is written
        # while a helper loads the config, and everything the installer does
        # afterwards would otherwise bury it.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()

            notice = Path(tmpdir) / GATEWAY_NOTICE_FILE
            self.assertIn("not root", notice.read_text(encoding="utf-8"))

    @patch("src.utils.config.ConfigManager._boot_id", return_value="boot-a")
    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_a_proven_port_clears_the_pending_notice(self, _euid, _boot):
        # Nothing left for the operator to do, so nothing left for the installer to
        # print on the next run.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.assign_gateway_port_if_unset()
            notice = Path(tmpdir) / GATEWAY_NOTICE_FILE
            self.assertTrue(notice.exists())

            manager.mark_gateway_port_passed(41000)

            self.assertFalse(notice.exists())


if __name__ == "__main__":
    unittest.main()
