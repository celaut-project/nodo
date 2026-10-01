"""``src/utils/keyvalue.py``: the one place the ``repeated *KeyValue`` fields are read
and written, and the ``service.json`` object syntax that survives the change.

The wire format and the service id are tested in ``tests/test_keyvalue_wire.py``.
"""
import json
import unittest

from tests.config_bootstrap import load_example_config

load_example_config()

from google.protobuf import json_format  # noqa: E402

from protos import celaut_pb2 as celaut, pack_pb2  # noqa: E402
from src.packers.service_json import (  # noqa: E402
    parse_service_spec,
    populate_possible_environment_workloads,
)
from src.utils import keyvalue  # noqa: E402
from src.utils.benchmark import parse_benchmark  # noqa: E402

PACKER_IMPORT_ERROR = None
try:
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    PACKER_IMPORT_ERROR = import_exc


def _wire(*entries):
    """A Contract holding these (key, value) xattr entries, in this order."""
    contract = celaut.Contract()
    for key, value in entries:
        entry = contract.xattrs.add()
        entry.key = key
        entry.value = value
    return contract


class ReadTests(unittest.TestCase):

    def test_to_dict_is_last_wins_like_a_map(self):
        contract = _wire(("k", b"first"), ("other", b"x"), ("k", b"last"))
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"k": b"last", "other": b"x"})

    def test_get_contains_and_default(self):
        contract = _wire(("k", b"first"), ("k", b"last"), ("empty", b""))
        self.assertEqual(keyvalue.get(contract.xattrs, "k"), b"last")
        self.assertEqual(keyvalue.get(contract.xattrs, "empty", b"default"), b"")
        self.assertEqual(keyvalue.get(contract.xattrs, "absent", b"default"), b"default")
        self.assertIsNone(keyvalue.get(contract.xattrs, "absent"))
        self.assertTrue(keyvalue.contains(contract.xattrs, "empty"))
        self.assertFalse(keyvalue.contains(contract.xattrs, "absent"))

    def test_reading_a_key_does_not_create_it(self):
        # The way `map[key]` did.
        contract = celaut.Contract()
        keyvalue.get(contract.xattrs, "k")
        keyvalue.contains(contract.xattrs, "k")
        self.assertEqual(len(contract.xattrs), 0)

    def test_items_and_keys_are_sorted_and_resolved(self):
        contract = _wire(("b", b"1"), ("a", b"2"), ("b", b"3"))
        self.assertEqual(keyvalue.items(contract.xattrs), [("a", b"2"), ("b", b"3")])
        self.assertEqual(keyvalue.keys(contract.xattrs), ("a", "b"))

    def test_message_values_are_returned_whole(self):
        peer = celaut.Peer()
        keyvalue.from_dict(peer.mu_per_call, {"exec": celaut.Amount(n="5")})
        self.assertEqual(keyvalue.get(peer.mu_per_call, "exec"), celaut.Amount(n="5"))

    def test_duplicate_keys(self):
        contract = _wire(("b", b"1"), ("a", b"2"), ("b", b"3"), ("a", b"4"), ("c", b"5"))
        self.assertEqual(keyvalue.duplicate_keys(contract.xattrs), ["a", "b"])
        with self.assertRaisesRegex(keyvalue.DuplicateKeyError, "xattrs repeats key.*a, b"):
            keyvalue.check_unique(contract.xattrs, "xattrs")
        keyvalue.check_unique(_wire(("a", b"1"), ("b", b"2")).xattrs)

    def test_is_canonical(self):
        self.assertTrue(keyvalue.is_canonical(_wire(("a", b""), ("b", b"")).xattrs))
        self.assertTrue(keyvalue.is_canonical(_wire().xattrs))
        self.assertFalse(keyvalue.is_canonical(_wire(("b", b""), ("a", b"")).xattrs))
        self.assertFalse(keyvalue.is_canonical(_wire(("a", b""), ("a", b"")).xattrs))
        self.assertFalse(keyvalue.is_canonical(_wire(("", b"")).xattrs))


class WriteTests(unittest.TestCase):

    def test_from_dict_writes_sorted_whatever_the_input_order(self):
        for order in (["b", "a", "c"], ["c", "b", "a"], ["a", "b", "c"]):
            contract = celaut.Contract()
            keyvalue.from_dict(contract.xattrs, {key: key.encode() for key in order})
            self.assertEqual([e.key for e in contract.xattrs], ["a", "b", "c"])

    def test_from_dict_replaces_what_was_there(self):
        contract = _wire(("old", b"1"))
        keyvalue.from_dict(contract.xattrs, {"new": b"2"})
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"new": b"2"})
        keyvalue.from_dict(contract.xattrs, {})
        self.assertEqual(len(contract.xattrs), 0)

    def test_set_value_updates_in_place_and_keeps_the_list_sorted(self):
        contract = celaut.Contract()
        keyvalue.set_value(contract.xattrs, "m", b"1")
        keyvalue.set_value(contract.xattrs, "a", b"2")
        keyvalue.set_value(contract.xattrs, "z", b"3")
        keyvalue.set_value(contract.xattrs, "m", b"4")
        self.assertEqual([(e.key, e.value) for e in contract.xattrs],
                         [("a", b"2"), ("m", b"4"), ("z", b"3")])

    def test_update_is_dict_update(self):
        contract = _wire(("a", b"1"), ("b", b"2"))
        keyvalue.update(contract.xattrs, {"b": b"3", "c": b"4"})
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"a": b"1", "b": b"3", "c": b"4"})
        self.assertTrue(keyvalue.is_canonical(contract.xattrs))

    def test_update_collapses_a_repeated_key_to_what_it_read_as(self):
        contract = _wire(("k", b"first"), ("k", b"last"))
        keyvalue.update(contract.xattrs, {"other": b"x"})
        self.assertEqual([(e.key, e.value) for e in contract.xattrs],
                         [("k", b"last"), ("other", b"x")])

    def test_delete_removes_every_entry_for_the_key(self):
        contract = _wire(("a", b"1"), ("k", b"first"), ("k", b"last"))
        self.assertTrue(keyvalue.delete(contract.xattrs, "k"))
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"a": b"1"})
        self.assertFalse(keyvalue.delete(contract.xattrs, "k"))
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"a": b"1"})

    def test_an_empty_value_and_a_zero_are_kept(self):
        contract = celaut.Contract()
        keyvalue.set_value(contract.xattrs, "k", b"")
        self.assertTrue(keyvalue.contains(contract.xattrs, "k"))
        self.assertTrue(contract.xattrs[0].HasField("value"))

        sysres = celaut.Sysresources()
        keyvalue.set_value(sysres.benchmark, "a", 0)
        self.assertEqual(keyvalue.get(sysres.benchmark, "a"), 0)
        self.assertTrue(sysres.benchmark[0].HasField("value"))

    def test_a_key_must_be_a_non_empty_string(self):
        contract = celaut.Contract()
        for bad in ("", b"bytes", None, 3):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    keyvalue.from_dict(contract.xattrs, {bad: b"v"})
                with self.assertRaises(ValueError):
                    keyvalue.set_value(contract.xattrs, bad, b"v")
        self.assertEqual(len(contract.xattrs), 0)

    def test_a_refused_write_leaves_the_list_as_it_was(self):
        contract = _wire(("a", b"1"))
        with self.assertRaises(ValueError):
            keyvalue.from_dict(contract.xattrs, {"b": b"2", "": b"3"})
        with self.assertRaises(ValueError):
            keyvalue.update(contract.xattrs, {"b": b"2", "": b"3"})
        self.assertEqual(keyvalue.to_dict(contract.xattrs), {"a": b"1"})

    def test_message_values_survive_update_and_delete(self):
        peer = celaut.Peer()
        keyvalue.from_dict(peer.mu_per_call, {
            "a": celaut.Amount(n="1"), "b": celaut.Amount(n="2"),
        })
        keyvalue.set_value(peer.mu_per_call, "c", celaut.Amount(n="3"))
        keyvalue.delete(peer.mu_per_call, "a")
        self.assertEqual(
            {k: v.n for k, v in keyvalue.items(peer.mu_per_call)}, {"b": "2", "c": "3"}
        )
        # The value is copied in: changing the source afterwards changes nothing.
        source = celaut.Amount(n="9")
        keyvalue.set_value(peer.mu_per_call, "d", source)
        source.n = "10"
        self.assertEqual(keyvalue.get(peer.mu_per_call, "d").n, "9")

    def test_from_dict_of_a_field_read_from_another_message(self):
        # The copy idiom used where a slot is filled from a peer.
        source = celaut.Peer()
        keyvalue.from_dict(source.mu_per_call, {"x": celaut.Amount(n="1")})
        target = celaut.Service.Api.Slot()
        keyvalue.from_dict(target.mu_per_call, keyvalue.to_dict(source.mu_per_call))
        self.assertEqual(target.mu_per_call[0].value.n, "1")


class ServiceJsonObjectSyntaxTests(unittest.TestCase):
    """service.json keeps ``{"KEY": value}``; the entry list is built when it is read."""

    def test_an_object_becomes_a_sorted_entry_list(self):
        service = parse_service_spec({"container": {
            "environment_variables": {
                "ZEBRA": {"prose": "z"}, "ALPHA": {"prose": "a", "tags": ["t"]},
            },
            "init": {"xattrs": {"b": "Yg==", "a": "YQ=="}},
            "resources": {"at_most": {"benchmark": {"int_ops_per_sec": 5, "a": "7"}}},
        }, "api": {"slot": [{"port": 1, "mu_per_call": {"rpc": {"n": "10"}, "abc": {"n": "5"}}}]}})

        self.assertEqual([e.key for e in service.container.environment_variables], ["ALPHA", "ZEBRA"])
        self.assertEqual(keyvalue.get(service.container.environment_variables, "ALPHA").prose, "a")
        self.assertEqual(keyvalue.to_dict(service.container.init.xattrs), {"a": b"a", "b": b"b"})
        self.assertEqual(
            keyvalue.to_dict(service.container.resources.at_most.benchmark),
            {"a": 7, "int_ops_per_sec": 5},
        )
        self.assertEqual([e.key for e in service.api.slot[0].mu_per_call], ["abc", "rpc"])

    def test_the_order_an_author_types_keys_in_does_not_reach_the_bytes(self):
        def parsed(mapping):
            return parse_service_spec({"container": {"init": {"xattrs": mapping}}})

        forward = parsed({"a": "MQ==", "b": "Mg==", "c": "Mw=="})
        backward = parsed({"c": "Mw==", "b": "Mg==", "a": "MQ=="})
        self.assertEqual(forward.SerializeToString(), backward.SerializeToString())

    def test_camel_case_field_names_are_found_too(self):
        service = parse_service_spec({"container": {"environmentVariables": {"A": {"prose": "p"}}}})
        self.assertEqual(keyvalue.get(service.container.environment_variables, "A").prose, "p")

    def test_the_protobuf_list_shape_is_accepted_and_sorted(self):
        service = parse_service_spec({"container": {"init": {"xattrs": [
            {"key": "b", "value": "Yg=="}, {"key": "a", "value": "YQ=="},
        ]}}})
        self.assertEqual([e.key for e in service.container.init.xattrs], ["a", "b"])

    def test_an_empty_value_written_explicitly_is_kept(self):
        service = parse_service_spec({"container": {"init": {"xattrs": {"k": ""}}}})
        self.assertTrue(service.container.init.xattrs[0].HasField("value"))
        again = celaut.Service()
        again.ParseFromString(service.SerializeToString())
        self.assertTrue(again.container.init.xattrs[0].HasField("value"))
        self.assertEqual(again.SerializeToString(), service.SerializeToString())

    def test_a_list_that_repeats_a_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "repeats key.*a"):
            parse_service_spec({"container": {"init": {"xattrs": [
                {"key": "a", "value": "MQ=="}, {"key": "a", "value": "Mg=="},
            ]}}})

    def test_an_object_that_repeated_a_key_in_the_file_is_refused(self):
        text = '{"container": {"init": {"xattrs": {"a": "MQ==", "b": "Mg==", "a": "Mw=="}}}}'
        document = json.loads(text, object_pairs_hook=keyvalue.json_object_hook)
        with self.assertRaisesRegex(ValueError, r"container\.init\.xattrs repeats key.*a"):
            parse_service_spec(document)
        # Read without the hook, the loader cannot know -- which is why the packer uses it.
        parse_service_spec(json.loads(text))

    def test_a_value_the_protobuf_parser_rejects_is_still_an_error(self):
        with self.assertRaises(ValueError):
            parse_service_spec({"container": {"resources": {"at_most": {
                "benchmark": {"int_ops_per_sec": "not a number"},
            }}}})

    def test_a_workload_group_resources_object(self):
        service = pack_pb2.Service()
        populate_possible_environment_workloads(service, [{"workloads": [{"count": 1, "resources": {
            "benchmark": {"z": 1, "a": 2},
        }}]}])
        resources = service.possible_environment_workload[0].workloads[0].resources
        self.assertEqual([e.key for e in resources.benchmark], ["a", "z"])

    def test_a_workload_group_that_repeats_a_key_is_refused(self):
        resources = json.loads(
            '{"benchmark": {"a": 1, "a": 2}}', object_pairs_hook=keyvalue.json_object_hook
        )
        with self.assertRaisesRegex(ValueError, "repeats key.*a"):
            populate_possible_environment_workloads(
                pack_pb2.Service(), [{"workloads": [{"count": 1, "resources": resources}]}]
            )

    def test_benchmark_refuses_a_repeated_key(self):
        value = json.loads('{"a": 1, "a": 2}', object_pairs_hook=keyvalue.json_object_hook)
        with self.assertRaisesRegex(ValueError, "resources.at_most.benchmark repeats key"):
            parse_benchmark(value, "resources.at_most.benchmark")
        self.assertEqual(parse_benchmark({"a": 1, "b": 2}, "p"), {"a": 1, "b": 2})

    def test_json_objects_to_entries_does_not_modify_its_input(self):
        document = {"container": {"init": {"xattrs": {"b": "Yg==", "a": "YQ=="}}}}
        keyvalue.json_objects_to_entries(document, celaut.Service.DESCRIPTOR)
        self.assertEqual(list(document["container"]["init"]["xattrs"]), ["b", "a"])


@unittest.skipIf(
    PACKER_IMPORT_ERROR is not None, f"Missing runtime dependencies: {PACKER_IMPORT_ERROR}"
)
class PackerTests(unittest.TestCase):

    @staticmethod
    def _packer(service_json_text):
        packer = ZipContainerPacker.__new__(ZipContainerPacker)
        packer.json = json.loads(service_json_text, object_pairs_hook=keyvalue.json_object_hook)
        packer.service = pack_pb2.Service()
        return packer

    def test_mu_per_call_is_packed_sorted_from_the_object(self):
        one = self._packer('{"api": [{"port": 8, "protocol": {"tags": ["p"]}, '
                           '"mu_per_call": {"b": 2, "a": 1, "c": 3}}]}')
        two = self._packer('{"api": [{"port": 8, "protocol": {"tags": ["p"]}, '
                           '"mu_per_call": {"c": 3, "a": 1, "b": 2}}]}')
        one.parseApi()
        two.parseApi()
        slot = one.service.api.slot[0]
        self.assertEqual([(e.key, e.value.n) for e in slot.mu_per_call],
                         [("a", "1"), ("b", "2"), ("c", "3")])
        self.assertEqual(one.service.SerializeToString(), two.service.SerializeToString())

    def test_mu_per_call_refuses_a_repeated_key(self):
        packer = self._packer('{"api": [{"port": 8, "protocol": {"tags": ["p"]}, '
                              '"mu_per_call": {"a": 1, "a": 2}}]}')
        with self.assertRaisesRegex(ValueError, r"api\[8\]\.mu_per_call repeats key.*a"):
            packer.parseApi()

    def test_init_xattrs_are_read_from_the_object(self):
        packer = self._packer('{"init": {"xattrs": {"b": "text", "a": "more"}}}')
        self.assertEqual(
            ZipContainerPacker._init_xattrs(packer.json["init"]), {"a": b"more", "b": b"text"}
        )
        keyvalue.from_dict(
            packer.service.container.init.xattrs, ZipContainerPacker._init_xattrs(packer.json["init"])
        )
        self.assertEqual([e.key for e in packer.service.container.init.xattrs], ["a", "b"])
        self.assertEqual(ZipContainerPacker._init_xattrs({}), {})

    def test_init_xattrs_refuse_a_repeated_key(self):
        packer = self._packer('{"init": {"xattrs": {"a": "1", "a": "2"}}}')
        with self.assertRaisesRegex(ValueError, r"init\.xattrs repeats key.*a"):
            ZipContainerPacker._init_xattrs(packer.json["init"])

    def test_benchmark_is_packed_sorted_and_refuses_a_repeated_key(self):
        packer = self._packer(
            '{"resources": {"at_most": {"benchmark": {"b_op": 2, "a_op": 1}}}}'
        )
        keyvalue.from_dict(
            packer.service.container.resources.at_most.benchmark, packer._benchmarks()[1]
        )
        self.assertEqual(
            [e.key for e in packer.service.container.resources.at_most.benchmark],
            ["a_op", "b_op"],
        )
        with self.assertRaisesRegex(ValueError, "benchmark repeats key"):
            self._packer(
                '{"resources": {"at_most": {"benchmark": {"a": 1, "a": 2}}}}'
            )._benchmarks()


class PublishedJsonTests(unittest.TestCase):
    """The Peer JSON published on-chain keeps the object shape a map produced."""

    def _peer(self):
        peer = celaut.Peer()
        keyvalue.from_dict(peer.mu_per_call, {
            "exec": celaut.Amount(n="10"), "build_mu": celaut.Amount(n="7"),
        })
        proof = peer.reputation_proofs.add()
        proof.ledger.tags.append("ergo")
        keyvalue.from_dict(proof.xattrs, {"token_id": b"\xaa", "script": b"\x01\x02"})
        return peer

    def test_key_value_fields_are_objects(self):
        published = json.loads(keyvalue.message_to_json(self._peer()))
        self.assertEqual(published["muPerCall"], {"build_mu": {"n": "7"}, "exec": {"n": "10"}})
        self.assertEqual(
            published["reputationProofs"][0]["xattrs"], {"script": "AQI=", "token_id": "qg=="}
        )

    def test_it_matches_what_protobuf_wrote_for_the_map(self):
        # Same document as MessageToJson of the map-based Peer, key for key.
        from tests import legacy_map_schema

        pool, _ = legacy_map_schema.build()
        old = legacy_map_schema.message_class(pool, "legacy_celaut.Peer")()
        old.mu_per_call["exec"].n = "10"
        old.mu_per_call["build_mu"].n = "7"
        proof = old.reputation_proofs.add()
        proof.ledger.tags.append("ergo")
        proof.xattrs["token_id"] = b"\xaa"
        proof.xattrs["script"] = b"\x01\x02"
        self.assertEqual(
            json.loads(keyvalue.message_to_json(self._peer())),
            json.loads(json_format.MessageToJson(old)),
        )

    def test_it_reads_back_through_the_object_syntax(self):
        peer = self._peer()
        document = keyvalue.message_to_dict(peer)
        again = celaut.Peer()
        json_format.ParseDict(
            keyvalue.json_objects_to_entries(document, celaut.Peer.DESCRIPTOR), again
        )
        self.assertEqual(again.SerializeToString(), peer.SerializeToString())

    def test_the_plain_protobuf_shape_is_a_list_and_reads_back_too(self):
        peer = self._peer()
        again = celaut.Peer()
        json_format.ParseDict(json_format.MessageToDict(peer), again)
        self.assertEqual(again.SerializeToString(), peer.SerializeToString())
        self.assertIsInstance(json_format.MessageToDict(peer)["muPerCall"], list)


if __name__ == "__main__":
    unittest.main()
