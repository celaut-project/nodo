"""``Gateway.ResolveNetwork`` may only be asked to *complete* a declaration (#385, §6).

Deferred resolution is the second half of templating: a network the node skipped at
launch because a ``${VAR}`` was unanswered is resolved later, over this RPC, once the
guest knows what it wants. That makes the RPC a way of asking for a network the
service never fully declared -- and before this change nothing checked the relation
between the two. A guest that declared ``pow:ergo`` pinned to block B could ask here
for ``pow:ergo`` pinned to nothing and be handed peers on any Ergo-shaped chain,
which is exactly the over-broad resolution the issue's motivating problem describes,
reached from the other direction.

The rule, and the direction of the asymmetry it encodes:

* narrowing is granted -- fill a templated key, add a key the author never wrote;
* broadening is refused -- drop a key the author fixed, or change one;
* the tags must be the same set, because a tag is what the operator's policy vetted
  and what dispatches the resolution.

``declared_networks_of_caller`` returning ``None`` (a caller this node cannot
identify as a local instance -- notably another *node* asking as a peer, which is the
RPC's older use) keeps the pre-#385 behaviour, and that is tested here too: the check
must not turn peer-to-peer resolution into a blanket refusal.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import component_formal
    from src.manager import networks as nets
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None
    nets = None


BLOCK = "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b"


def _network(tags, formal=None):
    network = celaut.Service.Network()
    network.tags.extend(tags)
    if formal is not None:
        network.formal = component_formal(formal) if isinstance(formal, dict) else formal
    return network


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RequestFitsDeclarationTests(unittest.TestCase):
    def _fits(self, declared, requested):
        return nets.request_fits_declaration(declared, requested)

    # ---------------------------------------------------------------- accepted

    def test_an_identical_request_fits(self):
        net = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        self.assertIsNone(self._fits(net, net))

    def test_a_templated_key_may_be_filled_with_a_concrete_value(self):
        declared = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${ERGO_BLOCK_ID}"}
        )
        requested = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK}
        )
        self.assertIsNone(self._fits(declared, requested))

    def test_a_templated_key_may_be_left_templated(self):
        # A caller narrowing in two steps, or relaying an ask it has not completed.
        declared = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${B}"})
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${C}"})
        self.assertIsNone(self._fits(declared, requested))

    def test_the_request_may_add_a_key_the_declaration_never_mentioned(self):
        """Adding always narrows: a reader either enforces a key or carries it."""
        declared = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        requested = _network(
            ["pow:ergo"],
            {
                "pow.chain": "ergo",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": "1152921504606846976",
                "pow.max_tip_age_s": "3600",
            },
        )
        self.assertIsNone(self._fits(declared, requested))

    def test_filling_and_adding_at_once_fits(self):
        declared = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${B}"}
        )
        requested = _network(
            ["pow:ergo"],
            {"pow.chain": "ergo", "pow.block_id": BLOCK, "pow.min_height": "1000000"},
        )
        self.assertIsNone(self._fits(declared, requested))

    def test_a_declaration_with_no_formal_accepts_any_formal_on_the_same_tags(self):
        # Nothing was fixed, so nothing can be contradicted. This is the "any peer
        # on this chain" a parent declares when it grants a whole chain.
        declared = _network(["pow:ergo"])
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        self.assertIsNone(self._fits(declared, requested))

    def test_tag_order_does_not_matter(self):
        declared = _network(["pow:ergo", "ergo"])
        requested = _network(["ergo", "pow:ergo"])
        self.assertIsNone(self._fits(declared, requested))

    # ---------------------------------------------------------------- refused

    def test_dropping_a_fixed_key_is_refused(self):
        """The refusal that matters most, because dropping looks harmless.

        ``pow.block_id`` removed turns "peers whose main chain contains B" into
        "any peer", which is a broader ask than the one the service declared.
        """
        declared = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        requested = _network(["pow:ergo"], {"pow.chain": "ergo"})
        reason = self._fits(declared, requested)
        self.assertIsNotNone(reason)
        self.assertIn("pow.block_id", reason)

    def test_changing_an_identity_key_is_refused(self):
        declared = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.consensus": "autolykos-v2"}
        )
        requested = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.consensus": "sha256d"}
        )
        reason = self._fits(declared, requested)
        self.assertIsNotNone(reason)
        self.assertIn("pow.consensus", reason)

    def test_changing_a_non_templated_selection_key_is_refused(self):
        declared = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        requested = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "a" * 64}
        )
        reason = self._fits(declared, requested)
        self.assertIsNotNone(reason)
        self.assertIn("pow.block_id", reason)

    def test_lowering_a_fixed_difficulty_is_refused_like_any_other_edit(self):
        # Nothing here interprets the *meaning* of a value, deliberately: this layer
        # compares declarations, and a domain's own vocabulary is the domain's.
        declared = _network(
            ["pow:ergo"],
            {"pow.chain": "ergo", "pow.min_cumulative_difficulty": "1152921504606846976"},
        )
        requested = _network(
            ["pow:ergo"], {"pow.chain": "ergo", "pow.min_cumulative_difficulty": "1"}
        )
        self.assertIsNotNone(self._fits(declared, requested))

    def test_a_different_tag_is_a_different_domain_not_a_narrowing(self):
        declared = _network(["pow:ergo"], {"pow.chain": "ergo"})
        requested = _network(["pow:bitcoin"], {"pow.chain": "ergo"})
        reason = self._fits(declared, requested)
        self.assertIsNotNone(reason)
        self.assertIn("pow:bitcoin", reason)

    def test_adding_a_tag_is_refused_even_though_adding_a_key_is_not(self):
        # A key narrows; a tag names another destination the policy never vetted.
        declared = _network(["pow:ergo"])
        requested = _network(["pow:ergo", "maps.google.com"])
        self.assertIsNotNone(self._fits(declared, requested))

    def test_a_request_whose_formal_cannot_be_read_is_refused_not_ignored(self):
        declared = _network(["x"], {"k": "v"})
        # No `=` anywhere, so `parse_component_formal` cannot read a pair out of it.
        requested = _network(["x"], b"not a pair")
        with self.assertRaises(nets.NetworkRequestRejected):
            self._fits(declared, requested)


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CheckNetworkRequestTests(unittest.TestCase):
    def test_fitting_any_one_declared_network_is_enough(self):
        declared = [
            _network(["dns:local"]),
            _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${B}"}),
        ]
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        nets.check_network_request(declared_networks=declared, requested=requested)

    def test_the_rejection_reports_every_declaration_it_was_measured_against(self):
        declared = [
            _network(["dns:local"]),
            _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK}),
        ]
        requested = _network(["pow:ergo"], {"pow.chain": "ergo"})
        with self.assertRaises(nets.NetworkRequestRejected) as caught:
            nets.check_network_request(declared_networks=declared, requested=requested)
        message = str(caught.exception)
        self.assertIn("dns:local", message)
        self.assertIn("pow.block_id", message)

    def test_a_service_declaring_no_network_is_refused_outright(self):
        # Otherwise the check is optional for anybody willing to declare nothing.
        with self.assertRaises(nets.NetworkRequestRejected):
            nets.check_network_request(
                declared_networks=[], requested=_network(["pow:ergo"])
            )


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CallerIdentificationTests(unittest.TestCase):
    """``None`` means "cannot tell who is asking", which is not "declared nothing"."""

    def test_an_unknown_address_is_unidentified_rather_than_empty(self):
        with patch.object(nets.sc, "get_local_instance_id_by_uri", return_value=None):
            self.assertIsNone(nets.declared_networks_of_caller("10.0.0.9"))

    def test_an_empty_address_is_unidentified(self):
        self.assertIsNone(nets.declared_networks_of_caller(""))

    def test_an_unreadable_spec_is_unidentified_and_logged(self):
        with patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value="container-1"
        ), patch.object(
            nets.sc, "get_service_id_by_container_id", return_value="svc-1"
        ), patch.object(
            nets, "load_service_from_disk", side_effect=OSError("locked")
        ), patch.object(nets, "LOGGER") as logger:
            self.assertIsNone(nets.declared_networks_of_caller("10.0.0.9"))
        self.assertTrue(logger.called)

    def test_an_identified_caller_yields_its_declared_networks(self):
        spec = celaut.Service()
        spec.network.add().tags.append("pow:ergo")
        with patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value="container-1"
        ), patch.object(
            nets.sc, "get_service_id_by_container_id", return_value="svc-1"
        ), patch.object(nets, "load_service_from_disk", return_value=spec):
            declared = nets.declared_networks_of_caller("10.0.0.9")
        self.assertEqual([list(n.tags) for n in declared], [["pow:ergo"]])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveNetworkForPeerTests(unittest.TestCase):
    def test_an_unidentified_caller_is_answered_as_before(self):
        """Another node asking as a peer has no spec here to be measured against."""
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=None
        ), patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value=None
        ), patch.object(nets, "resolve_network", return_value=[]) as resolve:
            resolution = nets.resolve_network_for_peer(requested, caller_ip="1.2.3.4")
        self.assertEqual(list(resolution.tags), ["pow:ergo"])
        resolve.assert_called_once()
        # Nobody to exclude from the answer: the caller is not an instance here.
        self.assertIsNone(resolve.call_args.kwargs["requester_id"])

    def test_a_local_caller_completing_its_own_template_is_answered(self):
        declared = [
            _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${B}"})
        ]
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=declared
        ), patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value="container-1"
        ), patch.object(
            nets.sc, "get_local_instance_envs", return_value=None
        ), patch.object(nets, "resolve_network", return_value=[]) as resolve, patch.object(
            nets, "grant_resolved_network"
        ) as grant:
            nets.resolve_network_for_peer(requested, caller_ip="10.0.0.9")
        resolve.assert_called_once()
        # A local caller is on the registry and must not be handed itself (#387).
        self.assertEqual(resolve.call_args.kwargs["requester_id"], "container-1")
        # #404: a request that fit the caller's own declaration is granted, not just
        # answered -- opened for the same VM the caller was identified as.
        grant.assert_called_once()
        self.assertEqual(grant.call_args.kwargs["vmachine_id"], "container-1")
        self.assertEqual(list(grant.call_args.kwargs["resolution"].tags), ["pow:ergo"])

    def test_an_unidentified_caller_is_never_granted(self):
        """No VM here to open anything on for a peer-to-peer caller (#404)."""
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=None
        ), patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value=None
        ), patch.object(nets, "resolve_network", return_value=[]), patch.object(
            nets, "grant_resolved_network"
        ) as grant:
            nets.resolve_network_for_peer(requested, caller_ip="1.2.3.4")
        grant.assert_not_called()

    def test_a_local_caller_broadening_its_own_ask_is_refused_before_resolving(self):
        declared = [
            _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        ]
        requested = _network(["pow:ergo"], {"pow.chain": "ergo"})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=declared
        ), patch.object(nets, "resolve_network") as resolve:
            with self.assertRaises(nets.NetworkRequestRejected):
                nets.resolve_network_for_peer(requested, caller_ip="10.0.0.9")
        resolve.assert_not_called()

    def test_a_request_that_is_still_templated_is_refused(self):
        """There is no peer holding a block named after a variable.

        Refused before the caller is even identified: it is a property of the
        request, and this node deliberately does not complete one from its own
        environment -- which instance of the protocol is meant is the caller's
        decision, which is the entire reason the key was left open.
        """
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": "${B}"})
        with patch.object(nets, "declared_networks_of_caller") as identify, patch.object(
            nets, "resolve_network"
        ) as resolve:
            with self.assertRaises(nets.NetworkRequestRejected) as caught:
                nets.resolve_network_for_peer(requested, caller_ip="10.0.0.9")
        self.assertIn("pow.block_id", str(caught.exception))
        identify.assert_not_called()
        resolve.assert_not_called()

    def _two_slot_instance(self):
        """One Instance shaped exactly as `resolve_pow_network` builds one: a P2P
        slot and a REST slot, tagged apart (issue #78)."""
        from src.manager.pow_networks import P2P_SLOT_TAG, REST_SLOT_TAG

        return celaut.Instance(
            api=celaut.Service.Api(slot=[
                celaut.Service.Api.Slot(
                    port=9030, protocol_stack=[celaut.Service.Api.Protocol(tags=[P2P_SLOT_TAG])]
                ),
                celaut.Service.Api.Slot(
                    port=9053, protocol_stack=[celaut.Service.Api.Protocol(tags=[REST_SLOT_TAG])]
                ),
            ]),
            uri_slot=[
                celaut.Instance.Uri_Slot(internal_port=9030, uri=[
                    celaut.Instance.Uri(ip="203.0.113.5", port=9030)
                ]),
                celaut.Instance.Uri_Slot(internal_port=9053, uri=[
                    celaut.Instance.Uri(ip="203.0.113.5", port=9053)
                ]),
            ],
        )

    def test_a_granted_local_caller_only_gets_the_slot_it_asked_for(self):
        """A bare pow:ergo ask never asked for a REST hole next to its chain peer
        (#404): what is answered to the guest, and what is opened for it, must both
        be narrowed the same way."""
        declared = [_network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})]
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=declared
        ), patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value="container-1"
        ), patch.object(
            nets.sc, "get_local_instance_envs", return_value=None
        ), patch.object(
            nets, "resolve_network", return_value=[self._two_slot_instance()]
        ), patch.object(nets, "grant_resolved_network") as grant:
            resolution = nets.resolve_network_for_peer(requested, caller_ip="10.0.0.9")

        self.assertEqual(len(resolution.peer_instances[0].api.slot), 1)
        granted = grant.call_args.kwargs["resolution"]
        self.assertEqual(len(granted.peer_instances[0].api.slot), 1)

    def test_an_unidentified_peer_gets_both_slots(self):
        """Nothing it does opens a firewall on the strength of this answer, so it
        gets the full picture -- this node's own peer discovery depends on it."""
        requested = _network(["pow:ergo"], {"pow.chain": "ergo", "pow.block_id": BLOCK})
        with patch.object(
            nets, "declared_networks_of_caller", return_value=None
        ), patch.object(
            nets.sc, "get_local_instance_id_by_uri", return_value=None
        ), patch.object(
            nets, "resolve_network", return_value=[self._two_slot_instance()]
        ):
            resolution = nets.resolve_network_for_peer(requested, caller_ip="1.2.3.4")

        self.assertEqual(len(resolution.peer_instances[0].api.slot), 2)

    def test_the_operator_policy_is_judged_before_the_declaration_is(self):
        """A caller learns "not from this node" before anything about its own spec.

        Order matters here: the policy is the operator's statement and applies to
        every caller, identified or not, and reversing the two would tell a rejected
        caller about a domain this node refuses to discuss at all.
        """
        from src.utils import network_policy as np

        requested = _network(["maps.google.com"])
        with patch.object(
            np.NetworkPolicy,
            "from_config",
            classmethod(
                lambda cls, env_manager=None: np.NetworkPolicy(blacklist=["*google.com"])
            ),
        ), patch.object(nets, "declared_networks_of_caller") as identify:
            with self.assertRaises(np.NetworkPolicyRejection):
                nets.resolve_network_for_peer(requested, caller_ip="10.0.0.9")
        identify.assert_not_called()


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GrantResolvedNetworkTests(unittest.TestCase):
    """The other half of deferred resolution: opening firewall, not just answering (#404)."""

    def _instance(self):
        instance = celaut.Instance()
        instance.uri_slot.add()
        return instance

    def test_a_wildcard_tag_allows_all_egress_instead_of_per_peer_rules(self):
        resolution = celaut.ConfigurationFile.NetworkResolution(tags=["*"])
        resolution.peer_instances.append(self._instance())
        with patch.object(
            nets, "allow_all_egress", return_value=True
        ) as allow_all, patch.object(
            nets, "allow_connection_to_instance"
        ) as allow_one:
            nets.grant_resolved_network(vmachine_id="vm-1", resolution=resolution)
        allow_all.assert_called_once_with(vmachine_id="vm-1")
        allow_one.assert_not_called()

    def test_every_peer_instance_gets_its_own_rule(self):
        resolution = celaut.ConfigurationFile.NetworkResolution(tags=["pow:ergo"])
        resolution.peer_instances.append(self._instance())
        resolution.peer_instances.append(self._instance())
        with patch.object(
            nets, "allow_connection_to_instance", return_value=True
        ) as allow_one:
            nets.grant_resolved_network(vmachine_id="vm-1", resolution=resolution)
        self.assertEqual(allow_one.call_count, 2)
        for call in allow_one.call_args_list:
            self.assertEqual(call.kwargs["vmachine_id"], "vm-1")

    def test_a_failed_rule_is_logged_not_raised(self):
        resolution = celaut.ConfigurationFile.NetworkResolution(tags=["pow:ergo"])
        resolution.peer_instances.append(self._instance())
        with patch.object(
            nets, "allow_connection_to_instance", return_value=False
        ), patch.object(nets, "LOGGER") as logger:
            nets.grant_resolved_network(vmachine_id="vm-1", resolution=resolution)
        self.assertTrue(logger.called)


if __name__ == "__main__":
    unittest.main()
