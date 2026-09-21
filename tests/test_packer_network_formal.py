"""`service.json`'s `network[].formal` / `network[].protocol_stack` -> `Service.Network`.

PR #366 made `Service.Network.formal` mean something: a `pow:` network's tag names
the *chain* and its `formal` says which instance of that chain is meant
(`pow.chain`, `pow.block_id`, `pow.min_cumulative_difficulty`, optionally
`pow.min_height` and `pow.max_tip_age_s`). The packer had no way to write one --
`parseNetwork` read `tags` and `prose` and dropped the rest of the entry -- so a
service like ergo-node-service could not *declare* `pow:ergo` at all, and the only
way to produce one was to hand-build the `Service` proto.

This pins the packer half of that contract:

* `formal` is a flat `{key: value}` object of **strings**, encoded by
  `node_identity.component_formal` -- the same sorted `key=value` body every other
  celaut descriptor uses, so two files declaring the same parameters in a different
  order pack to identical bytes. A JSON number is refused rather than stringified:
  cumulative work outgrew an IEEE double long ago.
* `protocol_stack` entries are tags/prose/formal descriptors, parsed by the same
  function that reads an api slot's `protocol` -- one message type, one parser.
* A `pow:` tag with a `formal` is run through `pow_networks.parse_pow_formal` at
  pack time, so a malformed ask fails while the author is looking at it instead of
  at a node launching the published service. Keys *outside* the `pow.` vocabulary
  are preserved, not refused: that is #366's contract, and the packer is not the
  ceiling on what a domain may say.
* **Absent `formal` serializes byte-identically to before.** The spec is hashed into
  the service id, so writing an empty field where there was none would have to be a
  no-op, and this asserts it against the bytes the old two-line `parseNetwork`
  produced.

`ZipContainerPacker.__init__` runs BuildKit, so it is never called here: the methods
under test are invoked on an object built with `__new__` carrying only the `json`
attribute they read. That is the whole of their input.
"""
import unittest

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example
    # points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import component_formal, parse_component_formal
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ZipContainerPacker = None  # type: ignore[assignment]


#: A well-formed `pow:ergo` ask, the shape ergo-node-service would ship.
ERGO_FORMAL = {
    "pow.chain": "ergo",
    "pow.block_id": "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b",
    "pow.min_cumulative_difficulty": "1152921504606846976",
}


def _packer(service_json):
    """A packer carrying only the `json` its network parsing reads."""
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


def _networks(service_json):
    return _packer(service_json)._parsed_networks()


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestNetworkFormalIsCarried(unittest.TestCase):
    """What `formal` in a service.json becomes in the packed spec."""

    def test_a_flat_string_map_becomes_component_formal_bytes(self):
        """The one encoder, not a second one written here."""
        network = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "an ergo node", "formal": ERGO_FORMAL},
        ]})[0]
        self.assertEqual(network.formal, component_formal(ERGO_FORMAL))
        self.assertEqual(parse_component_formal(network.formal), ERGO_FORMAL)

    def test_key_order_in_the_json_does_not_change_the_bytes(self):
        """`formal` is compared byte for byte, so it cannot depend on authoring order."""
        forwards = dict(sorted(ERGO_FORMAL.items()))
        backwards = dict(sorted(ERGO_FORMAL.items(), reverse=True))
        self.assertNotEqual(list(forwards), list(backwards))  # the input really differs

        def formal_of(pairs):
            return _networks({"network": [
                {"tags": ["pow:ergo"], "prose": "p", "formal": pairs},
            ]})[0].formal

        self.assertEqual(formal_of(forwards), formal_of(backwards))

    def test_tags_and_prose_are_still_read_the_same_way(self):
        network = _networks({"network": [
            {"tags": ["ipv4", "public"], "prose": "public internet"},
        ]})[0]
        self.assertEqual(list(network.tags), ["ipv4", "public"])
        self.assertEqual(network.prose, "public internet")

    def test_several_entries_are_each_carried(self):
        networks = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "chain", "formal": ERGO_FORMAL},
            {"tags": ["ipv4"], "prose": "internet"},
        ]})
        self.assertEqual(len(networks), 2)
        self.assertTrue(networks[0].formal)
        self.assertFalse(networks[1].formal)

    def test_a_missing_tags_key_still_raises(self):
        """Unchanged: `tags`/`prose` are read `[]`-style, only earlier now."""
        with self.assertRaises(KeyError):
            _networks({"network": [{"prose": "no tags"}]})

    def test_a_missing_prose_key_still_raises(self):
        with self.assertRaises(KeyError):
            _networks({"network": [{"tags": ["ipv4"]}]})


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestAbsentFormalIsByteIdentical(unittest.TestCase):
    """The service id of an existing service must not move.

    The whole `Service` spec is hashed into the id, so a network that declared no
    `formal` and no `protocol_stack` has to serialize to exactly the bytes the old
    two-line `parseNetwork` produced -- otherwise every service in existence gets a
    new id on its next repack, for no change in meaning.
    """

    def _legacy_network(self, entry):
        """What `parseNetwork` built before this change, verbatim."""
        network = celaut.Service.Network()
        network.tags.extend(entry['tags'])
        network.prose = entry['prose']
        return network

    def test_a_tags_and_prose_entry_serializes_to_the_old_bytes(self):
        entry = {"tags": ["ipv4", "public"], "prose": "public internet access"}
        self.assertEqual(
            _networks({"network": [entry]})[0].SerializeToString(),
            self._legacy_network(entry).SerializeToString(),
        )

    def test_the_whole_service_message_is_unchanged(self):
        """Not just the sub-message: what gets hashed is the service."""
        entries = [
            {"tags": ["ipv4", "public"], "prose": "public internet access"},
            {"tags": ["ipv4", "private"], "prose": "cluster network"},
        ]
        packed = celaut.Service()
        packed.network.extend(_networks({"network": entries}))

        legacy = celaut.Service()
        legacy.network.extend(self._legacy_network(e) for e in entries)

        self.assertEqual(packed.SerializeToString(), legacy.SerializeToString())

    def test_no_network_key_produces_no_networks(self):
        self.assertEqual(_networks({}), [])
        self.assertEqual(_networks({"network": []}), [])

    def test_an_absent_formal_is_not_written_as_empty_bytes(self):
        network = _networks({"network": [{"tags": ["ipv4"], "prose": "p"}]})[0]
        self.assertEqual(network.formal, b"")
        self.assertEqual(len(network.protocol_stack), 0)


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestFormalIsValidated(unittest.TestCase):
    """A malformed `formal` is a packer error naming the field, not coerced."""

    def _refused(self, entry):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [entry]})
        return str(caught.exception)

    def test_a_non_object_formal_is_refused(self):
        message = self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": "pow.chain=ergo"}
        )
        self.assertIn("network[0]", message)
        self.assertIn("formal", message)

    def test_a_list_formal_is_refused(self):
        self.assertIn("formal", self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": ["pow.chain=ergo"]}
        ))

    def test_a_numeric_value_is_refused_and_the_key_is_named(self):
        """JSON can hold a number; a formal body cannot hold it without rounding."""
        message = self._refused({
            "tags": ["ipv4"], "prose": "p",
            "formal": {"pow.min_cumulative_difficulty": 1152921504606846976},
        })
        self.assertIn("pow.min_cumulative_difficulty", message)
        self.assertIn("string", message)

    def test_a_boolean_value_is_refused(self):
        self.assertIn("enabled", self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": {"enabled": True}}
        ))

    def test_a_nested_object_value_is_refused(self):
        self.assertIn("pow", self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": {"pow": {"chain": "ergo"}}}
        ))

    def test_a_key_carrying_a_newline_is_refused(self):
        """Caught by the reader these bytes will actually be read with."""
        self.assertIn("formal", self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": {"a\nb": "1"}}
        ))

    def test_an_empty_key_is_refused(self):
        self.assertIn("formal", self._refused(
            {"tags": ["ipv4"], "prose": "p", "formal": {"": "1"}}
        ))

    def test_a_non_object_network_entry_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": ["ipv4"]})
        self.assertIn("network[0]", str(caught.exception))

    def test_the_index_of_the_offending_entry_is_named(self):
        message = self._refused_at_index_one()
        self.assertIn("network[1]", message)

    def _refused_at_index_one(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [
                {"tags": ["ipv4"], "prose": "fine"},
                {"tags": ["ipv4"], "prose": "p", "formal": {"k": 1}},
            ]})
        return str(caught.exception)


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestPowAsksAreParsedAtPackTime(unittest.TestCase):
    """A `pow:` ask is read by the launch-time parser while packing.

    Validation only -- nothing contacts a peer. What it buys is that a malformed
    ask fails at pack rather than at launch of a service that was already published.
    """

    def _refused(self, entry):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [entry]})
        return str(caught.exception)

    def test_a_well_formed_ergo_ask_packs(self):
        network = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "ergo peers", "formal": ERGO_FORMAL},
        ]})[0]
        self.assertEqual(parse_component_formal(network.formal), ERGO_FORMAL)

    def test_the_optional_keys_pack(self):
        formal = dict(ERGO_FORMAL, **{"pow.min_height": "1200000",
                                      "pow.max_tip_age_s": "3600"})
        network = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "p", "formal": formal},
        ]})[0]
        self.assertEqual(parse_component_formal(network.formal), formal)

    def test_a_missing_required_key_fails_at_pack(self):
        formal = {k: v for k, v in ERGO_FORMAL.items() if k != "pow.block_id"}
        message = self._refused({"tags": ["pow:ergo"], "prose": "p", "formal": formal})
        self.assertIn("pow.block_id", message)
        self.assertIn("pow:ergo", message)

    def test_a_non_hex_block_id_fails_at_pack(self):
        formal = dict(ERGO_FORMAL, **{"pow.block_id": "not-a-block"})
        self.assertIn("pow.block_id", self._refused(
            {"tags": ["pow:ergo"], "prose": "p", "formal": formal}
        ))

    def test_a_difficulty_that_is_not_an_integer_fails_at_pack(self):
        formal = dict(ERGO_FORMAL, **{"pow.min_cumulative_difficulty": "lots"})
        self.assertIn("pow.min_cumulative_difficulty", self._refused(
            {"tags": ["pow:ergo"], "prose": "p", "formal": formal}
        ))

    def test_a_tag_disagreeing_with_the_declared_chain_fails_at_pack(self):
        """The tag is what the operator's policy vetted; the two have to agree."""
        formal = dict(ERGO_FORMAL, **{"pow.chain": "bitcoin"})
        message = self._refused({"tags": ["pow:ergo"], "prose": "p", "formal": formal})
        self.assertIn("pow:ergo", message)

    def test_an_unknown_chain_fails_at_pack(self):
        formal = dict(ERGO_FORMAL, **{"pow.chain": "dogecoin"})
        formal["pow.chain"] = "dogecoin"
        self.assertIn("dogecoin", self._refused(
            {"tags": ["pow:dogecoin"], "prose": "p", "formal": formal}
        ))

    def test_keys_outside_the_pow_vocabulary_are_preserved_not_refused(self):
        """#366's contract: a reader is not the ceiling on what a domain may say."""
        formal = dict(ERGO_FORMAL, **{"vendor.note": "ergo-node-service",
                                      "ergo.api_version": "4"})
        network = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "p", "formal": formal},
        ]})[0]
        self.assertEqual(parse_component_formal(network.formal), formal)

    def test_a_pow_tag_with_no_formal_is_left_alone(self):
        """"Any peer on this chain" is what an ancestor granting the chain declares."""
        network = _networks({"network": [
            {"tags": ["pow:ergo"], "prose": "any ergo peer"},
        ]})[0]
        self.assertEqual(network.formal, b"")

    def test_a_formal_under_a_non_pow_tag_is_not_run_through_the_pow_parser(self):
        """Another domain's vocabulary is not this one's to validate."""
        formal = {"dns.name": "example.org"}
        network = _networks({"network": [
            {"tags": ["ipv4", "public"], "prose": "p", "formal": formal},
        ]})[0]
        self.assertEqual(parse_component_formal(network.formal), formal)


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestProtocolStack(unittest.TestCase):
    """`network[].protocol_stack`, read by the api slot's own descriptor parser."""

    def test_a_tag_list_entry_becomes_a_protocol(self):
        network = _networks({"network": [{
            "tags": ["pow:ergo"], "prose": "p",
            "protocol_stack": [["http"], ["ergo-node-api"]],
        }]})[0]
        self.assertEqual(
            [list(p.tags) for p in network.protocol_stack],
            [["http"], ["ergo-node-api"]],
        )

    def test_an_object_entry_carries_tags_prose_and_formal(self):
        network = _networks({"network": [{
            "tags": ["pow:ergo"], "prose": "p",
            "protocol_stack": [{
                "tags": ["ergo-node-api"],
                "prose": "the node's REST API",
                "formal": {"api.version": "4"},
            }],
        }]})[0]
        protocol = network.protocol_stack[0]
        self.assertEqual(list(protocol.tags), ["ergo-node-api"])
        self.assertEqual(protocol.prose, "the node's REST API")
        self.assertEqual(parse_component_formal(protocol.formal), {"api.version": "4"})

    def test_an_absent_protocol_stack_writes_nothing(self):
        network = _networks({"network": [{"tags": ["ipv4"], "prose": "p"}]})[0]
        self.assertEqual(len(network.protocol_stack), 0)

    def test_a_malformed_protocol_formal_is_refused_naming_its_position(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [{
                "tags": ["ipv4"], "prose": "p",
                "protocol_stack": [{"tags": ["http"]}, {"formal": {"v": 4}}],
            }]})
        message = str(caught.exception)
        self.assertIn("protocol_stack", message)
        self.assertIn("[1]", message)

    def test_non_string_tags_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [{
                "tags": ["ipv4"], "prose": "p",
                "protocol_stack": [{"tags": [4]}],
            }]})
        self.assertIn("tags", str(caught.exception))

    def test_a_non_string_prose_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [{
                "tags": ["ipv4"], "prose": "p",
                "protocol_stack": [{"tags": ["http"], "prose": 4}],
            }]})
        self.assertIn("prose", str(caught.exception))


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestApiSlotProtocolIsUnchanged(unittest.TestCase):
    """The shared parser must not move the bytes of an api slot packed today."""

    def test_a_tag_list_is_passed_through_as_before(self):
        packer = _packer({})
        self.assertEqual(
            packer._parse_protocol_descriptor(["http", "grpc"], "where")
            .SerializeToString(),
            celaut.Service.Api.Protocol(tags=["http", "grpc"]).SerializeToString(),
        )

    def test_an_absent_protocol_is_passed_through_as_before(self):
        """`item.get('protocol')` on a slot that declares none is `None`."""
        packer = _packer({})
        self.assertEqual(
            packer._parse_protocol_descriptor(None, "where").SerializeToString(),
            celaut.Service.Api.Protocol(tags=None).SerializeToString(),
        )


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestValidationRunsEarly(unittest.TestCase):
    """Refused in `__init__`, before BuildKit builds an image that cannot publish."""

    def test_the_shape_check_reads_the_networks(self):
        packer = _packer({"network": [
            {"tags": ["pow:ergo"], "prose": "p", "formal": {"pow.chain": "ergo"}},
        ]})
        with self.assertRaises(ValueError):
            packer._validate_service_json_shape()

    def test_a_valid_declaration_passes_the_shape_check(self):
        packer = _packer({"network": [
            {"tags": ["pow:ergo"], "prose": "p", "formal": ERGO_FORMAL},
        ]})
        packer._validate_service_json_shape()


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestNetworkPortsAreValidatedAtPackTime(unittest.TestCase):
    """A `port` in an entry's `formal` is read here before a node has to read it (#389).

    It is the one port a declaration states -- the standard port the hostname answers
    on -- and `networks.hostname_ports` grants nothing for a tag whose port it cannot
    read. A guest granted nothing looks exactly like a guest that asked for nothing, so
    the shape fails while the author is still looking at the file.
    """

    def _entry(self, port):
        return {"tags": ["api.example.test"], "prose": "an api", "formal": {"port": port}}

    def _refused(self, port):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [self._entry(port)]})
        return str(caught.exception)

    def test_a_port_packs(self):
        network = _networks({"network": [self._entry("50051")]})[0]
        self.assertEqual(parse_component_formal(network.formal), {"port": "50051"})

    def test_a_port_that_is_not_a_number_is_refused_and_the_entry_is_named(self):
        message = self._refused("https")
        self.assertIn("network[0]", message)
        self.assertIn("'https'", message)

    def test_a_port_outside_the_range_is_refused(self):
        self.assertIn("1-65535", self._refused("70000"))
        self.assertIn("1-65535", self._refused("0"))

    def test_a_templated_port_packs(self):
        """A selection key the instantiator fills (#385), checked once it is filled."""
        network = _networks({"network": [self._entry("${PORT}")]})[0]
        self.assertEqual(parse_component_formal(network.formal), {"port": "${PORT}"})

    def test_an_entry_saying_nothing_about_ports_packs(self):
        _networks({"network": [{"tags": ["www.example.test"], "prose": "a host"}]})
        _networks({"network": [
            {"tags": ["www.example.test"], "prose": "a host", "formal": {"api.version": "4"}},
        ]})


if __name__ == "__main__":
    unittest.main()
