"""What a guest's network policy actually consists of.

These matter more than they look. Deciding *which* rules to write is what
enforces isolation -- get it wrong and a guest's egress is silently open, which is
precisely the property the demo verifier's network_isolation probe exists to
catch. The virtualizer adapters need a database and a config and are therefore
skipped wherever those are missing, so the decision itself lives in pure
functions and is pinned here instead.

Each rule is asserted in both renderings, because the two backends must express
the same intent: a host that speaks nftables and one that speaks iptables have to
end up with the same policy.
"""
import unittest

from src.utils.firewall import policy
from src.utils.firewall.backends import _render_iptables, _render_nft
from src.utils.firewall.errors import RuleError
from src.utils.firewall.rules import Chain, Verdict

VM = "d1cc08a0fb1cac5cb725d76629b7e06c186c7f0aa04d65fe469356f7f437e3c8"
VM_IP = "192.168.200.148"
SUBNET = "192.168.200.0/24"


def iptables(rule):
    return " ".join(_render_iptables(rule))


class BlockAllTests(unittest.TestCase):
    def test_drops_new_connections_on_both_protocols(self):
        rules = policy.block_all_rules(vmachine_id=VM, vm_ip=VM_IP)
        self.assertEqual([rule.protocol for rule in rules], ["tcp", "udp"])
        for rule in rules:
            self.assertIs(rule.chain, Chain.FORWARD)
            self.assertIs(rule.verdict, Verdict.DROP)
            self.assertEqual(rule.ct_states, ("NEW",))
            self.assertEqual(rule.source, VM_IP)

    def test_renders_the_same_intent_in_both_backends(self):
        tcp = policy.block_all_rules(vmachine_id=VM, vm_ip=VM_IP)[0]
        self.assertEqual(
            iptables(tcp),
            f"-p tcp -s {VM_IP} -m conntrack --ctstate NEW -j DROP "
            f"-m comment --comment nodo;vm={VM};block_all;tcp",
        )
        self.assertEqual(
            _render_nft(tcp),
            f'ip saddr {VM_IP} meta l4proto tcp ct state new drop '
            f'comment "nodo;vm={VM};block_all;tcp"',
        )

    def test_a_malformed_address_never_reaches_the_firewall(self):
        for bad in ("", "not-an-ip", "192.168.200.999", "; rm -rf /"):
            with self.subTest(address=bad):
                with self.assertRaises(RuleError):
                    policy.block_all_rules(vmachine_id=VM, vm_ip=bad)


class AllowTests(unittest.TestCase):
    def test_an_allow_goes_above_the_blanket_drop(self):
        # block_all drops NEW; an allow added afterwards has to win, which is what
        # at_head preserves from the `iptables -I` this replaced.
        allow = policy.allow_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, ip="216.58.205.78", port=443, protocol="tcp"
        )
        self.assertTrue(allow.at_head)
        self.assertTrue(all(rule.at_head for rule in policy.block_all_rules(VM, VM_IP)))

    def test_carries_the_destination_and_port(self):
        allow = policy.allow_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, ip="216.58.205.78", port=443, protocol="tcp"
        )
        self.assertEqual(
            iptables(allow),
            f"-p tcp -s {VM_IP} -d 216.58.205.78 --dport 443 -j ACCEPT "
            f"-m comment --comment nodo;vm={VM};allow;216.58.205.78:443/tcp",
        )
        self.assertEqual(
            _render_nft(allow),
            f'ip saddr {VM_IP} ip daddr 216.58.205.78 tcp dport 443 accept '
            f'comment "nodo;vm={VM};allow;216.58.205.78:443/tcp"',
        )

    def test_a_portless_allow_covers_the_whole_host(self):
        allow = policy.allow_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, ip="216.58.205.78", protocol="tcp"
        )
        self.assertIsNone(allow.dport)
        self.assertIn("meta l4proto tcp", _render_nft(allow))
        self.assertNotIn("--dport", iptables(allow))

    def test_two_destinations_get_distinguishable_comments(self):
        first = policy.allow_connection_rule(VM, VM_IP, "1.1.1.1", 443, "tcp")
        second = policy.allow_connection_rule(VM, VM_IP, "1.1.1.1", 80, "tcp")
        third = policy.allow_connection_rule(VM, VM_IP, "1.1.1.1", 443, "udp")
        self.assertEqual(len({first.comment, second.comment, third.comment}), 3)

    def test_allow_all_egress_has_no_destination_at_all(self):
        rule = policy.allow_all_egress_rule(vmachine_id=VM, vm_ip=VM_IP)
        self.assertIsNone(rule.destination)
        self.assertIsNone(rule.protocol)
        self.assertTrue(rule.at_head)
        self.assertEqual(
            iptables(rule),
            f"-s {VM_IP} -j ACCEPT -m comment --comment nodo;vm={VM};allow_all_egress",
        )


class AllowHostTests(unittest.TestCase):
    """A guest reaching the host itself is an input-hook rule, not a forward one.

    The node's gateway port and the guest's resolver live on the bridge's gateway
    address, which is one of the host's own. A packet addressed there is delivered
    locally and never traverses forward -- so the allows nodo used to write for them
    (`allow_connection_rule`, chain FORWARD) could not match a single packet, while
    the log announced them as granted.
    """

    GATEWAY = "192.168.200.1"

    def test_the_rule_is_on_the_input_hook(self):
        rule = policy.allow_host_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, host_ip=self.GATEWAY, port=58443, protocol="tcp"
        )
        self.assertIs(rule.chain, Chain.INPUT)
        self.assertIs(rule.verdict, Verdict.ACCEPT)

    def test_it_is_scoped_to_one_guest_and_one_port(self):
        rule = policy.allow_host_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, host_ip=self.GATEWAY, port=58443, protocol="tcp"
        )
        self.assertEqual(rule.source, VM_IP)
        self.assertEqual(rule.destination, self.GATEWAY)
        self.assertEqual(rule.dport, 58443)
        # No conntrack state: it has to match every packet of the flow it permits,
        # because there is no input-side RELATED,ESTABLISHED accept behind it.
        self.assertEqual(rule.ct_states, ())

    def test_renders_the_same_intent_in_both_backends(self):
        rule = policy.allow_host_connection_rule(
            vmachine_id=VM, vm_ip=VM_IP, host_ip=self.GATEWAY, port=53, protocol="udp"
        )
        self.assertEqual(
            iptables(rule),
            f"-p udp -s {VM_IP} -d {self.GATEWAY} --dport 53 -j ACCEPT "
            f"-m comment --comment nodo;vm={VM};allow_host;{self.GATEWAY}:53/udp",
        )
        self.assertEqual(
            _render_nft(rule),
            f'ip saddr {VM_IP} ip daddr {self.GATEWAY} udp dport 53 accept '
            f'comment "nodo;vm={VM};allow_host;{self.GATEWAY}:53/udp"',
        )

    def test_its_comment_does_not_collide_with_the_forward_allow(self):
        # Both can coexist: an upgraded node still carries the old FORWARD rule for
        # instances that were already running when it restarted.
        host_rule = policy.allow_host_connection_rule(
            VM, VM_IP, self.GATEWAY, 58443, "tcp"
        )
        forward_rule = policy.allow_connection_rule(
            VM, VM_IP, self.GATEWAY, 58443, "tcp"
        )
        self.assertNotEqual(host_rule.comment, forward_rule.comment)

    def test_teardown_still_reaches_it_by_the_vm_prefix(self):
        rule = policy.allow_host_connection_rule(
            VM, VM_IP, self.GATEWAY, 58443, "tcp"
        )
        self.assertTrue(rule.comment.startswith(policy.vm_comment_prefix(VM)))

    def test_a_malformed_host_address_never_reaches_the_firewall(self):
        for bad in ("", "not-an-ip", "192.168.200.999", "; rm -rf /"):
            with self.subTest(address=bad):
                with self.assertRaises(RuleError):
                    policy.allow_host_connection_rule(
                        VM, VM_IP, bad, 58443, "tcp"
                    )


class MasqueradeTests(unittest.TestCase):
    def test_nats_the_subnet_only_on_the_way_out(self):
        rule = policy.masquerade_rule(SUBNET)
        self.assertIs(rule.chain, Chain.POSTROUTING)
        self.assertTrue(rule.destination_is_negated)
        self.assertFalse(rule.at_head)
        self.assertEqual(
            iptables(rule),
            f"-s {SUBNET} ! -d {SUBNET} -j MASQUERADE "
            f"-m comment --comment nodo;masquerade;subnet={SUBNET}",
        )
        self.assertEqual(
            _render_nft(rule),
            f'ip saddr {SUBNET} ip daddr != {SUBNET} masquerade '
            f'comment "nodo;masquerade;subnet={SUBNET}"',
        )

    def test_it_is_not_tied_to_any_vm(self):
        # Removing it during one instance's teardown would cut every other
        # instance's connectivity, so its comment must not carry a VM.
        self.assertNotIn(policy.VM_COMMENT_ROOT, policy.masquerade_rule(SUBNET).comment)

    def test_a_bare_address_is_refused(self):
        with self.assertRaises(RuleError):
            policy.masquerade_rule("192.168.200.1")


class PortForwardTests(unittest.TestCase):
    def setUp(self):
        self.rules = policy.port_forward_rules(
            vmachine_id=VM, vm_ip=VM_IP, protocol="tcp", external_port=59629, internal_port=5000
        )

    def test_publishes_a_port_with_the_two_rules_that_make_it_work(self):
        self.assertEqual(
            [rule.chain for rule in self.rules],
            [Chain.PREROUTING, Chain.FORWARD, Chain.FORWARD],
        )
        translation, inbound, replies = self.rules
        self.assertIs(translation.verdict, Verdict.DNAT)
        self.assertEqual(translation.dnat_to, f"{VM_IP}:5000")
        self.assertEqual(inbound.ct_states, ("NEW", "ESTABLISHED", "RELATED"))
        self.assertEqual(replies.ct_states, ("ESTABLISHED", "RELATED"))

    def test_appends_rather_than_inserting(self):
        # These are not policy exceptions racing the blanket drop; they belong
        # after it, exactly as the `-A` they replaced.
        self.assertFalse(any(rule.at_head for rule in self.rules))

    def test_renders_the_translation_identically_in_both_backends(self):
        translation = self.rules[0]
        self.assertEqual(
            iptables(translation),
            f"-p tcp --dport 59629 -j DNAT --to-destination {VM_IP}:5000 "
            f"-m comment --comment nodo;vm={VM};dnat;tcp;59629",
        )
        self.assertEqual(
            _render_nft(translation),
            f'tcp dport 59629 dnat to {VM_IP}:5000 comment "nodo;vm={VM};dnat;tcp;59629"',
        )

    def test_every_rule_is_reachable_from_the_vm_prefix(self):
        prefix = policy.vm_comment_prefix(VM)
        for rule in self.rules:
            self.assertTrue(rule.comment.startswith(prefix), rule.comment)

    def test_two_published_ports_do_not_collide(self):
        other = policy.port_forward_rules(VM, VM_IP, "tcp", 59630, 5001)
        self.assertEqual(
            len({rule.comment for rule in self.rules} | {rule.comment for rule in other}), 6
        )


class CommentTests(unittest.TestCase):
    def test_a_full_length_vm_id_still_fits_nfts_limit(self):
        # The real ids are 64-char hashes, and nft caps a comment at 128 bytes.
        rules = (
            policy.block_all_rules(VM, VM_IP)
            + [policy.allow_connection_rule(VM, VM_IP, "255.255.255.255", 65535, "udp")]
            + [policy.allow_all_egress_rule(VM, VM_IP)]
            + policy.port_forward_rules(VM, VM_IP, "udp", 65535, 65535)
        )
        for rule in rules:
            with self.subTest(comment=rule.comment):
                self.assertLessEqual(len(rule.comment), 127)

    def test_the_prefix_is_what_teardown_keys_on(self):
        prefix = policy.vm_comment_prefix(VM)
        self.assertTrue(prefix.startswith("nodo;vm="))
        self.assertTrue(prefix.endswith(";"))
        self.assertNotIn(prefix, policy.forward_related_established_rule().comment)

    def test_a_vm_without_an_id_is_refused(self):
        with self.assertRaises(RuleError):
            policy.vm_comment_prefix("   ")


class ReturnTrafficTests(unittest.TestCase):
    def test_the_blanket_accept_is_first_and_global(self):
        rule = policy.forward_related_established_rule()
        self.assertIs(rule.chain, Chain.FORWARD)
        self.assertTrue(rule.at_head)
        self.assertEqual(rule.ct_states, ("RELATED", "ESTABLISHED"))
        self.assertIsNone(rule.source)
        self.assertEqual(
            iptables(rule),
            "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT "
            "-m comment --comment nodo;forward;related_established",
        )


if __name__ == "__main__":
    unittest.main()
