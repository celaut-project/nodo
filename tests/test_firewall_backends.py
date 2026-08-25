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
from src.utils.firewall.rules import Chain, Rule, Verdict


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
            'nft insert rule inet nodo input tcp dport 58443 accept comment "%s"' % self.comment,
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
            [c for c in runner.commands() if "rule" in c and ("add" in c or "insert" in c)],
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
        scan = backend.foreign_input_rejectors()

        self.assertTrue(scan.readable)
        self.assertEqual(len(scan.rejectors), 1, scan)
        self.assertEqual(scan.rejectors[0].chain, "filter_INPUT")
        self.assertIn("firewalld", scan.rejectors[0].table)
        self.assertEqual(scan.rejectors[0].priority, 10)
        self.assertIn("reject", scan.rejectors[0].reason)

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
        scan = backend.foreign_input_rejectors()
        self.assertEqual(len(scan.rejectors), 1)
        self.assertIn("policy is drop", scan.rejectors[0].reason)

    def test_a_ruleset_that_could_not_be_read_is_not_a_clear_hook(self):
        # The difference the third state exists for. Docker's iptables-nft tables can
        # make the JSON listing fail while plain `nft list ruleset` works, and an
        # empty result there told the operator the input hook was clear.
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "ruleset"): _proc(1, "", "Error: no such file")})
        )
        scan = backend.foreign_input_rejectors()

        self.assertFalse(scan.readable)
        self.assertEqual(scan.rejectors, ())
        self.assertIn("nft -j list ruleset", scan.reason)
        self.assertIn("could not be determined", " ".join(scan.describe()))

    def test_unparseable_json_is_not_a_clear_hook_either(self):
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "ruleset"): _proc(0, "not json at all")})
        )
        scan = backend.foreign_input_rejectors()

        self.assertFalse(scan.readable)
        self.assertIn("could not be parsed", scan.reason)

    def test_a_genuinely_clear_hook_says_so(self):
        ruleset = {"nftables": [
            {"chain": {"family": "inet", "table": "nodo", "name": "input",
                       "type": "filter", "hook": "input", "prio": -5, "policy": "accept"}},
        ]}
        backend = NftBackend(
            run=FakeRunner({("nft", "-j", "list", "ruleset"): _proc(0, json.dumps(ruleset))})
        )
        scan = backend.foreign_input_rejectors()

        self.assertTrue(scan.readable)
        self.assertEqual(scan.rejectors, ())
        self.assertIn("Nothing outside nodo", " ".join(scan.describe()))


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

    def test_removal_replays_the_rule_iptables_itself_reported(self):
        listing = (
            f'-A INPUT -p tcp -m tcp --dport 58443 -m comment --comment "{self.comment}" -j ACCEPT\n'
        )
        runner = FakeRunner({("iptables", "-S", "INPUT"): _proc(0, listing)})
        backend = IptablesBackend(run=runner)

        [rule] = backend.list_input_accepts("nodo;gateway;port")
        backend.remove_input_accept(rule)

        # Byte-for-byte the line iptables printed, with -A swapped for -D: it matches
        # even when an older nodo wrote the rule with different argument order.
        self.assertIn(
            f'iptables -D INPUT -p tcp -m tcp --dport 58443 -m comment --comment {self.comment} -j ACCEPT',
            runner.commands(),
        )

    def test_a_rule_without_a_listing_cannot_be_deleted(self):
        backend = IptablesBackend(run=FakeRunner())
        with self.assertRaises(FirewallError):
            backend.remove_input_accept(
                InputRule(chain=Chain.INPUT, comment=self.comment, port=58443, protocol="tcp")
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


class NatChainTests(unittest.TestCase):
    """NAT lives in its own table, and iptables needs to be told which one."""

    def test_nft_puts_nat_in_the_ip_family_table(self):
        runner = FakeRunner({("nft", "-j", "list", "chain"): _proc(0, _nft_chain_listing([]))})
        backend = NftBackend(run=runner)
        backend.add(
            Rule(
                chain=Chain.POSTROUTING,
                comment="nodo;masquerade;subnet=192.168.200.0/24",
                verdict=Verdict.MASQUERADE,
                source="192.168.200.0/24",
                destination="192.168.200.0/24",
                destination_is_negated=True,
            )
        )
        commands = runner.commands()
        self.assertIn("nft add table ip nodo", commands)
        self.assertTrue(
            any("add chain ip nodo postrouting { type nat hook postrouting" in c for c in commands),
            commands,
        )

    def test_iptables_reaches_for_the_nat_table(self):
        runner = FakeRunner({("iptables", "-t", "nat", "-S"): _proc(0, "")})
        backend = IptablesBackend(run=runner)
        backend.add(
            Rule(
                chain=Chain.PREROUTING,
                comment="nodo;vm=v;dnat;tcp;59629",
                verdict=Verdict.DNAT,
                protocol="tcp",
                dport=59629,
                dnat_to="192.168.200.148:5000",
            )
        )
        self.assertIn(
            "iptables -t nat -A PREROUTING -p tcp --dport 59629 -j DNAT "
            "--to-destination 192.168.200.148:5000 -m comment --comment nodo;vm=v;dnat;tcp;59629",
            runner.commands(),
        )

    def test_filter_chains_do_not_get_the_nat_flag(self):
        runner = FakeRunner({("iptables", "-S"): _proc(0, "")})
        backend = IptablesBackend(run=runner)
        backend.add(Rule(chain=Chain.FORWARD, comment="nodo;vm=v;allow_all_egress", source="10.0.0.1"))
        self.assertTrue(
            all("-t" not in command for command in runner.calls),
            runner.commands(),
        )


class TeardownTests(unittest.TestCase):
    """A VM's whole footprint comes out by prefix, across every chain."""

    def test_every_chain_is_swept_for_the_prefix(self):
        prefix = "nodo;vm=abc;"
        listings = {
            Chain.FORWARD: [
                {"comment": prefix + "block_all;tcp", "handle": 1, "expr": []},
                {"comment": prefix + "dnat_in;tcp;5000", "handle": 2, "expr": []},
            ],
            Chain.PREROUTING: [{"comment": prefix + "dnat;tcp;59629", "handle": 3, "expr": []}],
        }

        def responder(command):
            command = list(command)
            if command[:4] == ["nft", "-j", "list", "chain"]:
                chain_name = command[-1]
                for chain, rules in listings.items():
                    if chain.value == chain_name:
                        return _proc(0, _nft_chain_listing(rules))
                return _proc(0, _nft_chain_listing([]))
            return _proc(0)

        runner = FakeRunner()
        runner.__call__ = None  # not used; responder drives it instead
        recorded = []

        def run(command):
            recorded.append(list(command))
            return responder(command)

        backend = NftBackend(run=run)
        removed = backend.delete_by_comment_prefix(prefix)

        self.assertEqual(removed, 3)
        deletes = [" ".join(c) for c in recorded if "delete" in c]
        self.assertEqual(len(deletes), 3)
        self.assertTrue(any("handle 3" in d for d in deletes), deletes)

    def test_another_vms_rules_are_left_alone(self):
        mine = "nodo;vm=mine;"
        runner = FakeRunner(
            {
                ("nft", "-j", "list", "chain"): _proc(
                    0,
                    _nft_chain_listing(
                        [
                            {"comment": mine + "block_all;tcp", "handle": 1, "expr": []},
                            {"comment": "nodo;vm=theirs;block_all;tcp", "handle": 2, "expr": []},
                        ]
                    ),
                )
            }
        )
        backend = NftBackend(run=runner)
        backend.delete_by_comment_prefix(mine, chains=(Chain.FORWARD,))

        deletes = [c for c in runner.commands() if "delete" in c]
        self.assertEqual(len(deletes), 1)
        self.assertIn("handle 1", deletes[0])


class EnsureFirstTests(unittest.TestCase):
    """The return-traffic accept is worthless unless it is actually first."""

    COMMENT = "nodo;forward;related_established"

    def _rule(self):
        return Rule(
            chain=Chain.FORWARD,
            comment=self.COMMENT,
            ct_states=("RELATED", "ESTABLISHED"),
            at_head=True,
        )

    def test_left_alone_when_it_is_already_on_top(self):
        runner = FakeRunner(
            {
                ("nft", "-j", "list", "chain"): _proc(
                    0,
                    _nft_chain_listing(
                        [
                            {"comment": self.COMMENT, "handle": 1, "expr": []},
                            {"comment": "nodo;vm=x;block_all;tcp", "handle": 2, "expr": []},
                        ]
                    ),
                )
            }
        )
        backend = NftBackend(run=runner)
        self.assertFalse(backend.ensure_first(self._rule()))
        self.assertFalse([c for c in runner.commands() if "insert" in c or "delete" in c])

    def test_relocated_when_something_sits_above_it(self):
        runner = FakeRunner(
            {
                ("nft", "-j", "list", "chain"): _proc(
                    0,
                    _nft_chain_listing(
                        [
                            {"comment": "nodo;vm=x;block_all;tcp", "handle": 2, "expr": []},
                            {"comment": self.COMMENT, "handle": 1, "expr": []},
                        ]
                    ),
                )
            }
        )
        backend = NftBackend(run=runner)
        self.assertTrue(backend.ensure_first(self._rule()))
        commands = runner.commands()
        self.assertTrue(any("delete" in c and "handle 1" in c for c in commands), commands)
        self.assertTrue(any("insert rule" in c for c in commands), commands)

    def test_duplicates_are_collapsed_to_one(self):
        runner = FakeRunner(
            {
                ("nft", "-j", "list", "chain"): _proc(
                    0,
                    _nft_chain_listing(
                        [
                            {"comment": self.COMMENT, "handle": 1, "expr": []},
                            {"comment": self.COMMENT, "handle": 5, "expr": []},
                        ]
                    ),
                )
            }
        )
        backend = NftBackend(run=runner)
        self.assertTrue(backend.ensure_first(self._rule()))
        deletes = [c for c in runner.commands() if "delete" in c]
        self.assertEqual(len(deletes), 2, deletes)
