"""The operator's network policy over ``Service.Network`` (issue #280).

``service_networks.blacklist`` / ``.whitelist`` decide which communication domains
this node is willing to run a service for. The policy is pure: it reads two lists
and judges a set of networks, so these tests construct :class:`NetworkPolicy`
directly and only reach for a fake config manager where reading the block itself is
what is under test.
"""
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils import network_policy as np
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None
    np = None


def _net(*tags):
    return celaut.Service.Network(tags=list(tags))


class FakeManager:
    """The one ``ConfigManager.get`` call ``from_config`` makes, over a dict."""

    def __init__(self, config):
        self.config = config

    def get(self, key, default=None):
        value = self.config.get(key)
        return value if value is not None else default


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NoPolicyTests(unittest.TestCase):
    def test_two_empty_lists_restrict_nothing(self):
        policy = np.NetworkPolicy()

        self.assertFalse(policy.restricts)
        self.assertIsNone(policy.check([_net("*"), _net("anything.at.all")]))

    def test_a_service_declaring_no_network_is_always_accepted(self):
        # Even under "refuse everything": it asked for no domain.
        policy = np.NetworkPolicy(blacklist=["*"])

        self.assertIsNone(policy.check([]))

    def test_a_network_with_no_tags_names_no_destination(self):
        policy = np.NetworkPolicy(blacklist=["*"])

        self.assertIsNone(policy.check([_net()]))

    def test_an_empty_tag_names_no_destination(self):
        policy = np.NetworkPolicy(blacklist=["*"])

        self.assertIsNone(policy.check([_net("", "   ")]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BlacklistTests(unittest.TestCase):
    def test_an_exact_tag_is_rejected(self):
        policy = np.NetworkPolicy(blacklist=["google.com"])

        rejection = policy.check([_net("google.com")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.tag, "google.com")
        self.assertEqual(rejection.pattern, "google.com")
        self.assertEqual(rejection.rule, np.BLACKLIST_KEY)

    def test_a_glob_covers_the_subdomains(self):
        policy = np.NetworkPolicy(blacklist=["*google.com"])

        self.assertIsNotNone(policy.check([_net("maps.google.com")]))

    def test_a_bare_domain_does_not_cover_its_subdomains(self):
        # Glob over the tag and nothing more: this is why config.example.yaml and
        # NETWORKS.md tell the operator to write "*google.com".
        policy = np.NetworkPolicy(blacklist=["google.com"])

        self.assertIsNone(policy.check([_net("maps.google.com")]))

    def test_matching_ignores_case_on_either_side(self):
        policy = np.NetworkPolicy(blacklist=["*GOOGLE.com"])

        self.assertIsNotNone(policy.check([_net("Maps.Google.COM")]))

    def test_star_refuses_every_service_that_declares_a_network(self):
        policy = np.NetworkPolicy(blacklist=["*"])

        self.assertIsNotNone(policy.check([_net("dns:local")]))
        self.assertIsNotNone(policy.check([_net("*")]))

    def test_any_tag_of_any_declared_network_is_enough_to_reject(self):
        policy = np.NetworkPolicy(blacklist=["pow:bitcoin"])

        rejection = policy.check([_net("dns:local"), _net("harmless", "pow:bitcoin")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.tag, "pow:bitcoin")
        self.assertEqual(rejection.network_index, 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WhitelistTests(unittest.TestCase):
    def test_a_covered_declaration_passes(self):
        policy = np.NetworkPolicy(whitelist=["dns:*", "pow:bitcoin"])

        self.assertIsNone(policy.check([_net("dns:local"), _net("pow:bitcoin")]))

    def test_an_uncovered_tag_is_rejected_and_named(self):
        policy = np.NetworkPolicy(whitelist=["dns:*"])

        rejection = policy.check([_net("pow:bitcoin")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.tag, "pow:bitcoin")
        self.assertIsNone(rejection.pattern)
        self.assertEqual(rejection.rule, np.WHITELIST_KEY)

    def test_every_tag_has_to_be_covered_not_just_one(self):
        # Each tag can open egress on its own -- resolve_network walks them one by
        # one -- so a network is only as allowed as its least allowed tag.
        policy = np.NetworkPolicy(whitelist=["dns:*"])

        rejection = policy.check([_net("dns:local", "www.google.com")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.tag, "www.google.com")

    def test_a_whitelist_that_covers_everything_allows_everything(self):
        policy = np.NetworkPolicy(whitelist=["*"])

        self.assertIsNone(policy.check([_net("anything"), _net("*")]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PrecedenceTests(unittest.TestCase):
    def test_the_blacklist_wins_on_a_tag_that_is_on_both_lists(self):
        policy = np.NetworkPolicy(blacklist=["dns:local"], whitelist=["dns:*"])

        rejection = policy.check([_net("dns:local")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.rule, np.BLACKLIST_KEY)

    def test_the_blacklist_wins_across_networks_not_only_within_one(self):
        # #1 misses the whitelist, #2 is blacklisted. The report names the
        # blacklist, which is the stronger statement about the request.
        policy = np.NetworkPolicy(blacklist=["pow:bitcoin"], whitelist=["dns:*"])

        rejection = policy.check([_net("www.google.com"), _net("pow:bitcoin")])

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.rule, np.BLACKLIST_KEY)
        self.assertEqual(rejection.tag, "pow:bitcoin")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReportTests(unittest.TestCase):
    def test_a_blacklist_report_names_the_rule_the_pattern_and_every_declaration(self):
        policy = np.NetworkPolicy(blacklist=["*google.com"])

        report = policy.check(
            [_net("maps.google.com", "dns:google"), _net("pow:bitcoin")],
            subject="service abc123",
        ).report()

        self.assertIn("service abc123", report)
        self.assertIn("maps.google.com (network #1)", report)
        self.assertIn("service_networks.blacklist", report)
        self.assertIn("*google.com", report)
        # Every declared network, not just the offending one.
        self.assertIn("dns:google", report)
        self.assertIn("pow:bitcoin", report)

    def test_a_whitelist_report_lists_what_the_tag_failed_to_match(self):
        policy = np.NetworkPolicy(whitelist=["dns:*", "pow:bitcoin"])

        report = policy.check([_net("www.google.com")]).report()

        self.assertIn("service_networks.whitelist", report)
        self.assertIn("matched none of:", report)
        self.assertIn("dns:*", report)
        self.assertIn("pow:bitcoin", report)

    def test_a_network_without_tags_is_still_reported_as_declared(self):
        policy = np.NetworkPolicy(blacklist=["pow:bitcoin"])

        report = policy.check([_net(), _net("pow:bitcoin")]).report()

        self.assertIn("<no tags>", report)

    def test_enforce_raises_the_report(self):
        with self.assertRaises(np.NetworkPolicyRejection) as ctx:
            np.enforce_network_policy(
                networks=[_net("google.com")],
                subject="service abc123",
                policy=np.NetworkPolicy(blacklist=["google.com"]),
            )

        self.assertIn("service_networks.blacklist", str(ctx.exception))
        self.assertEqual(ctx.exception.rejection.tag, "google.com")

    def test_enforce_is_silent_when_the_policy_allows(self):
        np.enforce_network_policy(
            networks=[_net("dns:local")],
            policy=np.NetworkPolicy(whitelist=["dns:*"]),
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigReadingTests(unittest.TestCase):
    def test_the_block_is_read_from_the_config(self):
        policy = np.NetworkPolicy.from_config(
            FakeManager({"service_networks": {"blacklist": ["*google.com"], "whitelist": ["dns:*"]}})
        )

        self.assertEqual(policy.blacklist, ("*google.com",))
        self.assertEqual(policy.whitelist, ("dns:*",))

    def test_an_absent_block_restricts_nothing(self):
        policy = np.NetworkPolicy.from_config(FakeManager({}))

        self.assertFalse(policy.restricts)

    def test_patterns_are_normalized_and_blanks_dropped(self):
        policy = np.NetworkPolicy.from_config(
            FakeManager({"service_networks": {"blacklist": ["  *GOOGLE.com ", "", None, 7]}})
        )

        self.assertEqual(policy.blacklist, ("*google.com", "7"))

    def test_a_bare_string_is_read_as_a_single_pattern(self):
        policy = np.NetworkPolicy.from_config(
            FakeManager({"service_networks": {"blacklist": "*"}})
        )

        self.assertEqual(policy.blacklist, ("*",))

    def test_a_list_this_node_cannot_read_is_not_a_list_that_allowed_everything(self):
        with self.assertRaises(np.NetworkPolicyConfigError):
            np.NetworkPolicy.from_config(
                FakeManager({"service_networks": {"blacklist": {"google.com": True}}})
            )

    def test_a_block_that_is_not_a_mapping_is_a_config_error(self):
        with self.assertRaises(np.NetworkPolicyConfigError):
            np.NetworkPolicy.from_config(FakeManager({"service_networks": ["*"]}))

    def test_the_policy_written_under_the_wrong_block_name_is_reported(self):
        # `networks:` is one letter from the unrelated `network:` block. Ignoring it
        # would leave the node with no policy while looking configured.
        with self.assertRaises(np.NetworkPolicyConfigError) as ctx:
            np.NetworkPolicy.from_config(
                FakeManager({"networks": {"blacklist": ["*google.com"]}})
            )

        self.assertIn("service_networks", str(ctx.exception))

    def test_an_unrelated_networks_block_is_left_alone(self):
        policy = np.NetworkPolicy.from_config(
            FakeManager({"networks": {"SOMETHING_ELSE": 1}})
        )

        self.assertFalse(policy.restricts)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ShippedDefaultTests(unittest.TestCase):
    def test_the_example_config_ships_a_policy_that_restricts_nothing(self):
        import os
        import yaml

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.yaml"), "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        block = config.get(np.CONFIG_BLOCK)
        self.assertIsNotNone(block, f"config.example.yaml has no '{np.CONFIG_BLOCK}:' block")
        self.assertEqual(block.get("blacklist"), [])
        self.assertEqual(block.get("whitelist"), [])
        self.assertFalse(np.NetworkPolicy(**block).restricts)


if __name__ == "__main__":
    unittest.main()
