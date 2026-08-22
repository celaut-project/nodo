"""Backend detection and rule management for the host firewall.

The point of the abstraction: on a host where ``iptables`` is the ``iptables-nft``
shim, rules written through it land in a compatibility table nodo does not own and
that an admin reading ``nft list ruleset`` never sees. Detection therefore prefers
native nftables whenever the host really speaks it.
"""
import json
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall.backends import (
    FirewallError,
    FirewallUnavailable,
    InputRule,
    IptablesBackend,
    NftBackend,
    detect_backend,
)
from src.utils.firewall.gateway import gateway_comment


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records every command and answers from a prefix -> result mapping."""

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default if default is not None else _proc(0)
        self.calls = []

    def __call__(self, command):
        command = list(command)
        self.calls.append(command)
        for prefix, result in self.responses.items():
            if command[: len(prefix)] == list(prefix):
                return result
        return self.default

    def commands(self):
        return [" ".join(call) for call in self.calls]


class BackendDetectionTests(unittest.TestCase):
    @patch("src.utils.firewall.backends.shutil.which", side_effect=lambda name: f"/usr/sbin/{name}")
    def test_nftables_wins_when_the_host_speaks_it(self, _which):
        runner = FakeRunner({("nft", "list", "tables"): _proc(0, "table inet firewalld\n")})
        self.assertIsInstance(detect_backend(run=runner), NftBackend)

    @patch("src.utils.firewall.backends.shutil.which", side_effect=lambda name: f"/usr/sbin/{name}")
    def test_falls_back_to_iptables_when_nft_is_unusable(self, _which):
        # nft is installed but the kernel or our privileges make it useless.
        runner = FakeRunner({("nft", "list", "tables"): _proc(1, "", "Operation not permitted")})
        self.assertIsInstance(detect_backend(run=runner), IptablesBackend)

    @patch("src.utils.firewall.backends.shutil.which", return_value=None)
    def test_raises_when_the_host_has_neither(self, _which):
        with self.assertRaises(FirewallUnavailable):
            detect_backend(run=FakeRunner())


def _nft_chain_listing(rules):
    return json.dumps({"nftables": [{"rule": rule} for rule in rules]})


class NftBackendTests(unittest.TestCase):
    def setUp(self):
        self.comment = gateway_comment(58443)

    def _backend(self, existing_rules=(), **extra):
        responses = {
            ("nft", "-j", "list", "chain"): _proc(0, _nft_chain_listing(list(existing_rules))),
        }
        responses.update(extra)
        runner = FakeRunner(responses)
        return NftBackend(run=runner), runner

    def test_adds_the_rule_when_absent(self):
        backend, runner = self._backend()
        self.assertTrue(
            backend.ensure_input_accept(port=58443, protocol="tcp", comment=self.comment)
        )
        commands = runner.commands()
        self.assertIn("nft add table inet nodo", commands)
        self.assertTrue(
            any(c.startswith("nft add chain inet nodo input {") for c in commands),
            commands,
        )
        self.assertIn(
            'nft add rule inet nodo input tcp dport 58443 accept comment "%s"' % self.comment,
            commands,
        )

    def test_is_a_no_op_when_the_rule_is_already_there(self):
        backend, runner = self._backend(
            existing_rules=[{"comment": self.comment, "handle": 7, "expr": []}]
        )
        self.assertFalse(
            backend.ensure_input_accept(port=58443, protocol="tcp", comment=self.comment)
        )
        self.assertFalse(
            [c for c in runner.commands() if c.startswith("nft add rule")],
            "an existing rule must not be duplicated",
        )

    def test_pruning_drops_other_ports_and_keeps_the_current_one(self):
        stale = gateway_comment(59110)
        backend, runner = self._backend(
            existing_rules=[
                {"comment": self.comment, "handle": 11, "expr": []},
                {"comment": stale, "handle": 12, "expr": []},
            ]
        )
        removed = backend.prune_input_accepts("nodo;gateway;port", keep=self.comment)

        self.assertEqual([rule.comment for rule in removed], [stale])
        self.assertIn("nft delete rule inet nodo input handle 12", runner.commands())
        self.assertNotIn("nft delete rule inet nodo input handle 11", runner.commands())

    def test_reads_the_port_back_out_of_the_rule_expression(self):
        backend, _ = self._backend(
            existing_rules=[
                {
                    "comment": self.comment,
                    "handle": 3,
                    "expr": [
                        {"match": {"left": {"payload": {"protocol": "tcp", "field": "dport"}},
                                   "right": 58443, "op": "=="}},
                        {"accept": None},
                    ],
                }
            ]
        )
        rules = backend.list_input_accepts("nodo;gateway;port")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].port, 58443)
        self.assertEqual(rules[0].protocol, "tcp")

    def test_no_table_yet_means_no_rules_rather_than_an_error(self):
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "chain"): _proc(1, "", "No such file or directory")})
        )
        self.assertEqual(backend.list_input_accepts("nodo;gateway;port"), [])

    def test_an_unreadable_chain_is_an_error_not_an_empty_list(self):
        # Mistaking "cannot read" for "no rules" would add a duplicate every start.
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "chain"): _proc(1, "", "Operation not permitted")})
        )
        with self.assertRaises(FirewallError):
            backend.list_input_accepts("nodo;gateway;port")

    def test_finds_the_foreign_chain_that_can_reject(self):
        ruleset = {
            "nftables": [
                {"chain": {"family": "inet", "table": "firewalld", "name": "filter_INPUT",
                           "type": "filter", "hook": "input", "prio": 10, "policy": "accept"}},
                {"chain": {"family": "inet", "table": "nodo", "name": "input",
                           "type": "filter", "hook": "input", "prio": -5, "policy": "accept"}},
                {"rule": {"family": "inet", "table": "firewalld", "chain": "filter_INPUT",
                          "expr": [{"reject": {"type": "icmpx"}}]}},
                {"rule": {"family": "inet", "table": "nodo", "chain": "input",
                          "expr": [{"accept": None}]}},
            ]
        }
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "ruleset"): _proc(0, json.dumps(ruleset))})
        )
        rejectors = backend.foreign_input_rejectors()

        self.assertEqual(len(rejectors), 1, rejectors)
        self.assertEqual(rejectors[0].chain, "filter_INPUT")
        self.assertIn("firewalld", rejectors[0].table)
        self.assertEqual(rejectors[0].priority, 10)
        self.assertIn("reject", rejectors[0].reason)

    def test_a_chain_whose_policy_is_drop_counts_too(self):
        ruleset = {
            "nftables": [
                {"chain": {"family": "ip", "table": "filter", "name": "INPUT",
                           "type": "filter", "hook": "input", "prio": 0, "policy": "drop"}},
            ]
        }
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "ruleset"): _proc(0, json.dumps(ruleset))})
        )
        rejectors = backend.foreign_input_rejectors()
        self.assertEqual(len(rejectors), 1)
        self.assertIn("policy is drop", rejectors[0].reason)


class IptablesBackendTests(unittest.TestCase):
    def setUp(self):
        self.comment = gateway_comment(58443)

    def test_parses_its_own_rules_out_of_the_save_output(self):
        listing = (
            "-P INPUT ACCEPT\n"
            "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
            f'-A INPUT -p tcp -m tcp --dport 58443 -m comment --comment "{self.comment}" -j ACCEPT\n'
        )
        backend = IptablesBackend(
            run=FakeRunner({("iptables", "-S", "INPUT"): _proc(0, listing)})
        )
        rules = backend.list_input_accepts("nodo;gateway;port")

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].port, 58443)
        self.assertEqual(rules[0].protocol, "tcp")
        self.assertEqual(rules[0].comment, self.comment)

    def test_an_unreadable_input_chain_is_an_error(self):
        backend = IptablesBackend(
            run=FakeRunner({("iptables", "-S", "INPUT"): _proc(3, "", "Permission denied")})
        )
        with self.assertRaises(FirewallError):
            backend.list_input_accepts("nodo;gateway;port")

    def test_inserts_at_the_head_with_a_comment(self):
        backend = IptablesBackend(run=(runner := FakeRunner({("iptables", "-S"): _proc(0, "")})))
        backend.ensure_input_accept(port=58443, protocol="tcp", comment=self.comment)

        self.assertIn(
            "iptables -I INPUT -p tcp --dport 58443 -j ACCEPT -m comment --comment "
            + self.comment,
            runner.commands(),
        )

    def test_removal_mirrors_the_insert(self):
        runner = FakeRunner()
        backend = IptablesBackend(run=runner)
        backend.remove_input_accept(
            InputRule(comment=self.comment, port=58443, protocol="tcp")
        )
        self.assertIn(
            "iptables -D INPUT -p tcp --dport 58443 -j ACCEPT -m comment --comment "
            + self.comment,
            runner.commands(),
        )

    def test_a_failing_insert_is_an_error_not_a_silent_pass(self):
        backend = IptablesBackend(
            run=FakeRunner(
                {("iptables", "-I"): _proc(1, "", "Permission denied")},
                default=_proc(0, ""),
            )
        )
        with self.assertRaises(FirewallError) as ctx:
            backend.ensure_input_accept(port=58443, protocol="tcp", comment=self.comment)
        self.assertIn("Permission denied", str(ctx.exception))


class ValidationTests(unittest.TestCase):
    def test_rejects_unusable_ports_and_protocols(self):
        backend = NftBackend(run=FakeRunner())
        for port, protocol in ((0, "tcp"), (70000, "tcp"), ("58443", "tcp"), (58443, "sctp")):
            with self.subTest(port=port, protocol=protocol):
                with self.assertRaises(FirewallError):
                    backend.ensure_input_accept(
                        port=port, protocol=protocol, comment="nodo;gateway;port=x"
                    )


if __name__ == "__main__":
    unittest.main()
