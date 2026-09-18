"""A ``pow:`` ask may leave its selection keys open (#385, §2 and §5).

``parse_pow_formal`` is the one validator that runs at *both* ends of a service's
life: at pack time, on what the author wrote, and at launch time, on what the node is
about to go looking for. Templating means those two want opposite things from the
same function -- the packer must accept ``pow.block_id=${ERGO_BLOCK_ID}`` as a
perfectly good declaration, and the resolver must refuse it, because there is no peer
holding a block named after a variable.

That is the ``allow_templates`` flag, and the default is the strict one, so the
refusal is what a caller gets by forgetting about this rather than by remembering.

Also pinned here:

* ``pow.chain`` is never templatable, whatever the flag says -- it is the identity
  key the operator's policy vetted through the tag, and choosing a chain after the
  policy ran is choosing one nobody vetted;
* everything else about a body is still validated when a template is present, so a
  templated ask that is also malformed still fails the pack;
* the packer refuses a value that is only *partly* a placeholder.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import component_formal
    from src.manager.pow_networks import (
        PowFormalError,
        PowRequirement,
        canonical_formal,
        parse_pow_formal,
    )
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ZipContainerPacker = None


BLOCK = "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b"
DIFF = "1152921504606846976"


def _packer(service_json):
    """A packer carrying only the ``json`` its network parsing reads.

    ``__init__`` runs BuildKit, so it is never called: the methods under test read
    one attribute, and that attribute is the whole of their input.
    """
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ParsePowFormalTemplateTests(unittest.TestCase):
    def test_a_templated_selection_key_parses_when_templates_are_allowed(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": "${ERGO_BLOCK_ID}",
                "pow.min_cumulative_difficulty": DIFF,
            }
        )
        req = parse_pow_formal(formal, tag="pow:ergo", allow_templates=True)
        self.assertEqual(req.chain, "ergo")
        self.assertIsNone(req.block_id)
        self.assertEqual(req.min_cumulative_difficulty, int(DIFF))
        self.assertEqual(req.templated, ("pow.block_id",))
        self.assertFalse(req.is_complete)

    def test_every_typed_key_may_be_templated(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": "${B}",
                "pow.min_cumulative_difficulty": "${D}",
                "pow.min_height": "${H}",
                "pow.max_tip_age_s": "${A}",
            }
        )
        req = parse_pow_formal(formal, tag="pow:ergo", allow_templates=True)
        self.assertEqual(
            req.templated,
            (
                "pow.block_id",
                "pow.max_tip_age_s",
                "pow.min_cumulative_difficulty",
                "pow.min_height",
            ),
        )
        self.assertIsNone(req.block_id)
        self.assertIsNone(req.min_cumulative_difficulty)
        self.assertIsNone(req.min_height)
        self.assertIsNone(req.max_tip_age_s)

    def test_the_default_refuses_a_template_so_a_resolver_cannot_get_one(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": "${B}",
                "pow.min_cumulative_difficulty": DIFF,
            }
        )
        with self.assertRaises(PowFormalError) as caught:
            parse_pow_formal(formal, tag="pow:ergo")
        self.assertIn("pow.block_id", str(caught.exception))

    def test_pow_chain_is_never_templatable(self):
        formal = component_formal(
            {
                "pow.chain": "${CHAIN}",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": DIFF,
            }
        )
        with self.assertRaises(PowFormalError) as caught:
            parse_pow_formal(formal, allow_templates=True)
        self.assertIn("identity key", str(caught.exception))

    def test_a_templated_body_is_still_validated_in_every_other_way(self):
        # Missing `pow.chain` entirely: templating one key does not excuse the rest.
        formal = component_formal(
            {"pow.block_id": "${B}", "pow.min_cumulative_difficulty": DIFF}
        )
        with self.assertRaises(PowFormalError):
            parse_pow_formal(formal, allow_templates=True)

        # And a tag that disagrees with the chain still fails.
        formal = component_formal(
            {
                "pow.chain": "bitcoin",
                "pow.block_id": "${B}",
                "pow.min_cumulative_difficulty": DIFF,
            }
        )
        with self.assertRaises(PowFormalError):
            parse_pow_formal(formal, tag="pow:ergo", allow_templates=True)

    def test_a_concrete_body_parses_identically_under_either_flag(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": DIFF,
                "pow.min_height": "1000000",
            }
        )
        strict = parse_pow_formal(formal, tag="pow:ergo")
        lenient = parse_pow_formal(formal, tag="pow:ergo", allow_templates=True)
        self.assertEqual(strict, lenient)
        self.assertEqual(strict.templated, ())
        self.assertTrue(strict.is_complete)

    def test_extensions_may_be_templated_and_are_carried_as_written(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": DIFF,
                "ledger.model": "${MODEL}",
            }
        )
        req = parse_pow_formal(formal, tag="pow:ergo", allow_templates=True)
        # Outside the `pow.` vocabulary, so it is carried, not interpreted -- and
        # `templated` names only this module's own keys.
        self.assertEqual(req.extensions["ledger.model"], "${MODEL}")
        self.assertEqual(req.templated, ())


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CanonicalFormalTests(unittest.TestCase):
    def test_a_concrete_requirement_still_round_trips_byte_for_byte(self):
        formal = component_formal(
            {
                "pow.chain": "ergo",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": DIFF,
            }
        )
        self.assertEqual(canonical_formal(parse_pow_formal(formal)), formal)

    def test_a_templated_requirement_refuses_to_be_re_serialized(self):
        """It does not carry the variable *names*, so it cannot write them back.

        The dangerous alternative is dropping the key: ``pow.block_id`` silently
        vanishing turns "contains block B" into "any peer".
        """
        req = PowRequirement(
            chain="ergo",
            block_id=None,
            min_cumulative_difficulty=None,
            templated=("pow.block_id", "pow.min_cumulative_difficulty"),
        )
        with self.assertRaises(PowFormalError):
            canonical_formal(req)


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackerTemplateTests(unittest.TestCase):
    def test_a_templated_pow_ask_packs(self):
        networks = _packer(
            {
                "network": [
                    {
                        "tags": ["pow:ergo"],
                        "prose": "Ergo, chain picked by the instantiator",
                        "formal": {
                            "pow.chain": "ergo",
                            "pow.block_id": "${ERGO_BLOCK_ID}",
                            "pow.min_cumulative_difficulty": "${ERGO_MIN_CUMULATIVE_DIFFICULTY}",
                            "pow.min_height": "${ERGO_MIN_HEIGHT}",
                            "pow.max_tip_age_s": "3600",
                        },
                    }
                ]
            }
        )._parsed_networks()

        self.assertEqual(len(networks), 1)
        self.assertIn(b"pow.block_id=${ERGO_BLOCK_ID}", networks[0].formal)

    def test_the_packed_bytes_are_canonical_with_templates_present(self):
        """Authoring order must not change the bytes: this field is hashed.

        The same guarantee as an untemplated body, asserted with templates in it
        because `${...}` is where a hand-rolled encoder would have been tempting.
        """
        formal = {
            "pow.min_cumulative_difficulty": "${D}",
            "pow.chain": "ergo",
            "pow.block_id": "${B}",
        }
        reordered = {
            "pow.block_id": "${B}",
            "pow.chain": "ergo",
            "pow.min_cumulative_difficulty": "${D}",
        }
        a = _packer(
            {"network": [{"tags": ["pow:ergo"], "prose": "", "formal": formal}]}
        )._parsed_networks()[0]
        b = _packer(
            {"network": [{"tags": ["pow:ergo"], "prose": "", "formal": reordered}]}
        )._parsed_networks()[0]
        self.assertEqual(a.formal, b.formal)
        self.assertEqual(
            a.formal,
            component_formal(
                {
                    "pow.chain": "ergo",
                    "pow.block_id": "${B}",
                    "pow.min_cumulative_difficulty": "${D}",
                }
            ),
        )

    def test_an_untemplated_network_packs_byte_identically_to_before(self):
        """The regression guard for every service that exists today."""
        formal = {
            "pow.chain": "ergo",
            "pow.block_id": BLOCK,
            "pow.min_cumulative_difficulty": DIFF,
        }
        network = _packer(
            {"network": [{"tags": ["pow:ergo"], "prose": "p", "formal": formal}]}
        )._parsed_networks()[0]
        self.assertEqual(network.formal, component_formal(formal))

    def test_a_partial_placeholder_is_refused_at_pack_time(self):
        with self.assertRaises(ValueError) as caught:
            _packer(
                {
                    "network": [
                        {
                            "tags": ["pow:ergo"],
                            "prose": "",
                            "formal": {
                                "pow.chain": "ergo",
                                "pow.block_id": "abc${ERGO_BLOCK_ID}",
                                "pow.min_cumulative_difficulty": DIFF,
                            },
                        }
                    ]
                }
            )._parsed_networks()
        message = str(caught.exception)
        self.assertIn("pow.block_id", message)
        self.assertIn("entirely a placeholder", message)

    def test_a_partial_placeholder_is_refused_outside_the_pow_vocabulary_too(self):
        # The grammar belongs to the field, not to one domain's vocabulary.
        with self.assertRaises(ValueError):
            _packer(
                {
                    "network": [
                        {
                            "tags": ["my-domain"],
                            "prose": "",
                            "formal": {"anything": "v${X}"},
                        }
                    ]
                }
            )._parsed_networks()

    def test_a_templated_pow_chain_still_fails_the_pack(self):
        with self.assertRaises(ValueError) as caught:
            _packer(
                {
                    "network": [
                        {
                            "tags": ["pow:ergo"],
                            "prose": "",
                            "formal": {
                                "pow.chain": "${CHAIN}",
                                "pow.block_id": BLOCK,
                                "pow.min_cumulative_difficulty": DIFF,
                            },
                        }
                    ]
                }
            )._parsed_networks()
        self.assertIn("pow.chain", str(caught.exception))

    def test_a_malformed_templated_ask_still_fails_the_pack(self):
        # Templating one key does not turn off the rest of the validation.
        with self.assertRaises(ValueError):
            _packer(
                {
                    "network": [
                        {
                            "tags": ["pow:ergo"],
                            "prose": "",
                            "formal": {
                                "pow.chain": "ergo",
                                "pow.block_id": "${B}",
                                "pow.min_cumulative_difficulty": "not-an-integer",
                            },
                        }
                    ]
                }
            )._parsed_networks()


if __name__ == "__main__":
    unittest.main()
