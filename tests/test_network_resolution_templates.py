"""Per-network eager/deferred resolution at launch (issue #385, §6).

``build_network_resolution`` is where a ``${VAR}`` stops being syntax and becomes a
decision about *when* a network is resolved. Four cases, and the fourth is the one
that keeps the feature from being a security hole:

1. every placeholder answered -> substitute and resolve, and the resolver is handed
   the **completed** ask, not the templated one;
2. any placeholder unanswered -> that network alone is skipped, the others resolve,
   and nothing raises;
3. no placeholder at all -> byte-identical behaviour to before this existed;
4. an operator policy violation still aborts, templated or not -- template completion
   must not be able to turn a refusal into a launch or vice versa.

``resolve_network`` is patched throughout: what it does with a formal is
``test_pow_networks.py``'s subject, and what is asserted here is *which* formal it is
given and *whether* it is called at all.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import component_formal, parse_component_formal
    from src.utils import network_policy as np
    from src.virtualizers.microvm import rootfs as microvm_rootfs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None
    np = None
    microvm_rootfs = None


TEMPLATED = {
    "pow.chain": "ergo",
    "pow.block_id": "${ERGO_BLOCK_ID}",
    "pow.min_cumulative_difficulty": "${ERGO_MIN_CUMULATIVE_DIFFICULTY}",
}

CONCRETE = {
    "pow.chain": "ergo",
    "pow.block_id": "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b",
    "pow.min_cumulative_difficulty": "1152921504606846976",
}


def _service(*networks):
    service = celaut.Service()
    for tags, formal in networks:
        network = service.network.add()
        network.tags.extend(tags)
        if formal is not None:
            network.formal = formal
    return service


def _config(**env):
    config = celaut.Configuration()
    for key, value in env.items():
        config.environment_variables[key] = value
    return config


def _policy(**kwargs):
    """The same shim ``test_network_policy_enforcement`` uses: force ``from_config``."""
    return patch.object(
        np.NetworkPolicy,
        "from_config",
        classmethod(lambda cls, env_manager=None: np.NetworkPolicy(**kwargs)),
    )


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EagerResolutionTests(unittest.TestCase):
    def test_a_network_with_no_template_resolves_exactly_as_before(self):
        formal = component_formal(CONCRETE)
        service = _service((["pow:ergo"], formal))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]) as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        self.assertEqual([list(r.tags) for r in resolution], [["pow:ergo"]])
        # Byte-identical, and the *same* declaration object: nothing copied it and
        # nothing re-encoded it, which is what keeps a pre-#385 service's behaviour
        # bit for bit the same.
        self.assertEqual(resolve.call_args.args[0].formal, formal)

    def test_a_network_with_no_formal_at_all_still_resolves(self):
        service = _service((["dns:local"], None))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]) as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=None
            )

        self.assertEqual(len(resolution), 1)
        resolve.assert_called_once()

    def test_an_untagged_network_is_skipped_as_it_always_was(self):
        service = _service(([], component_formal(CONCRETE)))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        self.assertEqual(resolution, [])
        resolve.assert_not_called()

    def test_a_pow_ergo_instance_is_narrowed_to_the_slot_this_guest_asked_for(self):
        """The eager, launch-time path -- the common one, unlike the deferred
        Gateway.ResolveNetwork RPC -- must narrow too: `resolve_pow_network` builds
        both a P2P and a REST slot on every Instance (issue #78), and this guest's
        firewall (`configure_guest_firewall_policy`) opens a rule per uri it is
        handed. A bare pow:ergo declaration asked for a chain peer, not a REST hole
        granted next to it (#404)."""
        from src.manager.pow_networks import P2P_SLOT_TAG, REST_SLOT_TAG

        two_slot_instance = celaut.Instance(
            api=celaut.Service.Api(slot=[
                celaut.Service.Api.Slot(
                    port=9030,
                    protocol_stack=[celaut.Service.Api.Protocol(tags=[P2P_SLOT_TAG])],
                ),
                celaut.Service.Api.Slot(
                    port=9053,
                    protocol_stack=[celaut.Service.Api.Protocol(tags=[REST_SLOT_TAG])],
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
        service = _service((["pow:ergo"], component_formal(CONCRETE)))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            microvm_rootfs, "resolve_network", return_value=[two_slot_instance]
        ):
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        self.assertEqual(len(resolution[0].peer_instances[0].api.slot), 1)
        self.assertEqual(len(resolution[0].peer_instances[0].uri_slot), 1)
        self.assertEqual(
            list(resolution[0].peer_instances[0].api.slot[0].protocol_stack[0].tags),
            [P2P_SLOT_TAG],
        )


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TemplateCompletionTests(unittest.TestCase):
    def test_a_fully_answered_template_resolves_with_the_completed_formal(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))
        config = _config(
            ERGO_BLOCK_ID=CONCRETE["pow.block_id"].encode(),
            ERGO_MIN_CUMULATIVE_DIFFICULTY=CONCRETE[
                "pow.min_cumulative_difficulty"
            ].encode(),
        )

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]) as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=config
            )

        self.assertEqual([list(r.tags) for r in resolution], [["pow:ergo"]])
        self.assertEqual(
            parse_component_formal(resolve.call_args.args[0].formal), CONCRETE
        )

    def test_completion_does_not_mutate_the_declaration_it_came_from(self):
        """The spec this node stores has to keep saying what it said.

        ``filter_networks_with_ancestors`` re-derives a grant from ancestors' *specs*
        at every launch, so a substitution that edited the declaration in place would
        change what a later generation is authorized against.
        """
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))
        declared_before = bytes(service.network[0].formal)
        config = _config(
            ERGO_BLOCK_ID=b"deadbeef", ERGO_MIN_CUMULATIVE_DIFFICULTY=b"1"
        )

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]):
            microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=config
            )

        self.assertEqual(bytes(service.network[0].formal), declared_before)

    def test_the_resolution_carries_the_declared_tags(self):
        service = _service((["pow:ergo", "ergo"], component_formal(TEMPLATED)))
        config = _config(
            ERGO_BLOCK_ID=b"ab", ERGO_MIN_CUMULATIVE_DIFFICULTY=b"1"
        )

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]):
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=config
            )

        self.assertEqual(list(resolution[0].tags), ["pow:ergo", "ergo"])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeferredResolutionTests(unittest.TestCase):
    def test_a_missing_variable_omits_that_network_and_does_not_raise(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        self.assertEqual(resolution, [])
        resolve.assert_not_called()

    def test_one_answered_variable_is_not_enough_when_another_is_not(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))
        config = _config(ERGO_BLOCK_ID=b"deadbeef")

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=config
            )

        self.assertEqual(resolution, [])
        resolve.assert_not_called()

    def test_a_deferred_network_does_not_affect_its_siblings(self):
        """The skip is per network, which is the whole of the policy's shape."""
        service = _service(
            (["pow:ergo"], component_formal(TEMPLATED)),
            (["dns:local"], None),
            (["pow:bitcoin"], component_formal({"pow.chain": "bitcoin"})),
        )

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network", return_value=[]) as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        self.assertEqual(
            [list(r.tags) for r in resolution], [["dns:local"], ["pow:bitcoin"]]
        )
        self.assertEqual(resolve.call_count, 2)

    def test_no_config_at_all_defers_a_templated_network(self):
        # `config=None` is a legitimate launch shape; it answers no variable.
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=None
            )

        self.assertEqual(resolution, [])
        resolve.assert_not_called()

    def test_the_skip_names_the_variables_in_the_log(self):
        """The one outcome that looks like a bug from inside the guest.

        It boots fine and finds ``__config__`` short an entry it declared, so the
        reason has to be somewhere -- and the node's log is the only place it can be.
        """
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network"), patch.object(
            microvm_rootfs.log, "LOGGER"
        ) as logger:
            microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=_config()
            )

        messages = " ".join(str(call.args[0]) for call in logger.call_args_list)
        self.assertIn("ERGO_BLOCK_ID", messages)
        self.assertIn("ERGO_MIN_CUMULATIVE_DIFFICULTY", messages)
        self.assertIn("pow:ergo", messages)

    def test_a_value_that_cannot_live_in_a_formal_defers_rather_than_aborting(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))
        config = _config(
            ERGO_BLOCK_ID=b"deadbeef",
            ERGO_MIN_CUMULATIVE_DIFFICULTY=b"1\npow.min_height=0",
        )

        with patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            resolution = microvm_rootfs.build_network_resolution(
                service=service, father_id="", config=config
            )

        self.assertEqual(resolution, [])
        resolve.assert_not_called()


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PolicyStillAbortsTests(unittest.TestCase):
    """Whether a launch is refused must not depend on which variables are set.

    The policy is judged on the *declared* network, before any substitution, so an
    operator who blacklists a tag refuses it templated, filled, and deferred alike.
    Were it judged after, a service could evade a blacklist by leaving a variable
    unset -- the network would be dropped before the policy ever saw it.
    """

    def test_a_forbidden_tag_aborts_even_when_the_template_is_unanswered(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))

        with _policy(blacklist=["pow:*"]), patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            with self.assertRaises(np.NetworkPolicyRejection):
                microvm_rootfs.build_network_resolution(
                    service=service, father_id="", config=_config()
                )

        resolve.assert_not_called()

    def test_a_forbidden_tag_aborts_when_the_template_is_answered_too(self):
        service = _service((["pow:ergo"], component_formal(TEMPLATED)))
        config = _config(
            ERGO_BLOCK_ID=b"deadbeef", ERGO_MIN_CUMULATIVE_DIFFICULTY=b"1"
        )

        with _policy(blacklist=["pow:*"]), patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            with self.assertRaises(np.NetworkPolicyRejection):
                microvm_rootfs.build_network_resolution(
                    service=service, father_id="", config=config
                )

        resolve.assert_not_called()

    def test_a_deferred_network_still_blocks_a_sibling_from_launching(self):
        # The abort is the launch's, not the network's: one rejected declaration
        # refuses the whole service, which is what "the only abort path" means.
        service = _service(
            (["pow:ergo"], component_formal(TEMPLATED)),
            (["maps.google.com"], None),
        )

        with _policy(blacklist=["*google.com"]), patch.object(
            microvm_rootfs.sc, "internal_instance_exists", return_value=False
        ), patch.object(microvm_rootfs, "resolve_network") as resolve:
            with self.assertRaises(np.NetworkPolicyRejection):
                microvm_rootfs.build_network_resolution(
                    service=service, father_id="", config=_config()
                )

        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
