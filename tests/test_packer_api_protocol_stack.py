"""`service.json`'s `api[].protocol_stack` -> `Service.Api.Slot.protocol_stack`.

`api[].protocol` gives a slot exactly one protocol layer. A convention that sits on
top of HTTP needs two: the chat convention of chatui (docs/celaut/chat.md) is a
slot with `http`, and above it `chat`, and the client refuses a slot whose single
layer has both tags. `api[].protocol_stack` declares the layers from the bottom up,
with the same descriptor parser as `network[].protocol_stack`.

`protocol` must keep its bytes: the spec is hashed into the service id.

`ZipContainerPacker.__init__` runs BuildKit, so it is never called here: `parseApi`
runs on an object built with `__new__` that carries only `json` and `service`.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import parse_component_formal
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ZipContainerPacker = None  # type: ignore[assignment]


def _slots(api):
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = {"api": api}
    packer.service = celaut.Service()
    packer.parseApi()
    return list(packer.service.api.slot)


def _stack(slot):
    return [list(protocol.tags) for protocol in slot.protocol_stack]


@unittest.skipIf(ZipContainerPacker is None, f"packer import failed: {IMPORT_ERROR}")
class TestApiProtocolStack(unittest.TestCase):

    def test_the_layers_are_kept_in_order(self):
        slot = _slots([{"port": 8080, "transport": "tcp",
                        "protocol_stack": [["http"], ["chat"]]}])[0]
        self.assertEqual(_stack(slot), [["http"], ["chat"]])

    def test_an_object_layer_carries_tags_prose_and_formal(self):
        slot = _slots([{"port": 8080, "protocol_stack": [
            ["http"],
            {"tags": ["chat"], "prose": "an echo", "formal": {"chat.max": "10000"}},
        ]}])[0]
        chat = slot.protocol_stack[1]
        self.assertEqual(list(chat.tags), ["chat"])
        self.assertEqual(chat.prose, "an echo")
        self.assertEqual(parse_component_formal(chat.formal), {"chat.max": "10000"})

    def test_protocol_keeps_its_bytes(self):
        """One layer, as before, for a tag list and for an absent key."""
        for item, tags in (({"port": 1, "protocol": ["http", "grpc"]}, ["http", "grpc"]),
                           ({"port": 1}, None)):
            expected = celaut.Service.Api.Slot(port=1)
            expected.transport.tags.append("tcp")
            expected.protocol_stack.append(celaut.Service.Api.Protocol(tags=tags))
            self.assertEqual(_slots([item])[0].SerializeToString(),
                             expected.SerializeToString())

    def test_one_layer_stack_equals_protocol(self):
        by_stack = _slots([{"port": 1, "protocol_stack": [["http"]]}])[0]
        by_protocol = _slots([{"port": 1, "protocol": ["http"]}])[0]
        self.assertEqual(by_stack.SerializeToString(), by_protocol.SerializeToString())

    def test_both_keys_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            _slots([{"port": 8080, "protocol": ["http"],
                     "protocol_stack": [["http"], ["chat"]]}])
        self.assertIn("not both", str(caught.exception))

    def test_an_empty_or_non_list_stack_is_refused(self):
        for stack in ([], ["http"][0], {"tags": ["http"]}, None):
            with self.assertRaises(ValueError, msg=repr(stack)):
                _slots([{"port": 8080, "protocol_stack": stack}])

    def test_a_bad_layer_is_refused_naming_its_position(self):
        with self.assertRaises(ValueError) as caught:
            _slots([{"port": 8080, "protocol_stack": [["http"], {"tags": [4]}]}])
        self.assertIn("'protocol_stack'[1]", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
