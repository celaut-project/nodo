import subprocess
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers import firewall as vm_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    vm_firewall = None  # type: ignore[assignment]


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtualizerFirewallGlobalRuleTests(unittest.TestCase):
    def test_ensure_forward_related_established_rule_inserts_when_missing(self):
        with patch("src.virtualizers.firewall.subprocess.run") as run_mock:
            run_mock.side_effect = [
                _cp(
                    ["iptables", "-S", "FORWARD"],
                    stdout="-A FORWARD -j DROP\n",
                ),
                _cp(["iptables", "-I", "FORWARD", "1"]),
            ]

            result = vm_firewall.ensure_forward_related_established_rule()

        self.assertTrue(result)
        self.assertEqual(run_mock.call_count, 2)
        insert_cmd = run_mock.call_args_list[1].args[0]
        self.assertEqual(insert_cmd[:4], ["iptables", "-I", "FORWARD", "1"])
        self.assertIn("--ctstate", insert_cmd)
        self.assertIn("RELATED,ESTABLISHED", insert_cmd)
        self.assertIn("--comment", insert_cmd)
        self.assertIn(vm_firewall.FORWARD_RELATED_ESTABLISHED_COMMENT, insert_cmd)

    def test_ensure_forward_related_established_rule_no_duplicate_when_already_top(self):
        existing_top = (
            '-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT '
            '-m comment --comment "nodo;forward;related_established"'
        )

        with patch("src.virtualizers.firewall.subprocess.run") as run_mock:
            run_mock.side_effect = [
                _cp(["iptables", "-S", "FORWARD"], stdout=f"{existing_top}\n-A FORWARD -j DROP\n"),
            ]

            result = vm_firewall.ensure_forward_related_established_rule()

        self.assertTrue(result)
        self.assertEqual(run_mock.call_count, 1)

    def test_ensure_forward_related_established_rule_relocates_and_deduplicates(self):
        first = '-A FORWARD -j DROP'
        dup_one = (
            '-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT '
            '-m comment --comment "nodo;legacy"'
        )
        dup_two = (
            '-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT '
            '-m comment --comment "nodo;forward;related_established"'
        )

        with patch("src.virtualizers.firewall.subprocess.run") as run_mock:
            run_mock.side_effect = [
                _cp(["iptables", "-S", "FORWARD"], stdout=f"{first}\n{dup_one}\n{dup_two}\n"),
                _cp(["iptables", "-D", "FORWARD"]),
                _cp(["iptables", "-D", "FORWARD"]),
                _cp(["iptables", "-I", "FORWARD", "1"]),
            ]

            result = vm_firewall.ensure_forward_related_established_rule()

        self.assertTrue(result)

        commands = [call.args[0] for call in run_mock.call_args_list]
        delete_commands = [cmd for cmd in commands if len(cmd) > 2 and cmd[1] == "-D" and cmd[2] == "FORWARD"]
        insert_commands = [cmd for cmd in commands if len(cmd) > 3 and cmd[1:4] == ["-I", "FORWARD", "1"]]

        self.assertEqual(len(delete_commands), 2)
        self.assertEqual(len(insert_commands), 1)


if __name__ == "__main__":
    unittest.main()
