import sys
import types
import unittest
from unittest import mock

if "mnemonic" not in sys.modules:
    mnemonic_module = types.ModuleType("mnemonic")

    class _Mnemonic:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, strength=128):
            return "test mnemonic"

    mnemonic_module.Mnemonic = _Mnemonic
    sys.modules["mnemonic"] = mnemonic_module

from src.reputation_system.contracts.ergo import transaction


class _FakeSenderAddress:
    def asP2PK(self):
        return "sender-p2pk"


class _FakeBuilder:
    def __init__(self, accept_list=True, accept_add_list=True):
        self.accept_list = accept_list
        self.accept_add_list = accept_add_list
        self.calls = []

    def boxesToSpend(self, boxes):
        self.calls.append(("boxesToSpend", boxes))
        return self

    def outputs(self, outputs):
        self.calls.append(("outputs", outputs))
        return self

    def fee(self, fee):
        self.calls.append(("fee", fee))
        return self

    def sendChangeTo(self, address):
        self.calls.append(("sendChangeTo", address))
        return self

    def withDataInputs(self, *args):
        if len(args) == 1 and isinstance(args[0], list) and self.accept_list:
            self.calls.append(("withDataInputs", args[0]))
            return self
        raise TypeError("list form unsupported")

    def addDataInputs(self, *args):
        if len(args) == 1 and isinstance(args[0], list) and not self.accept_add_list:
            raise TypeError("addDataInputs list form unsupported")
        self.calls.append(("addDataInputs", list(args)))
        return self

    def build(self):
        self.calls.append(("build", None))
        return "built-directly"


class _FakeCtx:
    def __init__(self, builder):
        self.builder = builder

    def newTxBuilder(self):
        return self.builder


class _FakeErgo:
    def __init__(self, error_on_kwargs=("dataInput", "dataInputs"), builder=None):
        self.error_on_kwargs = set(error_on_kwargs)
        self.builder = builder or _FakeBuilder()
        self._ctx = _FakeCtx(self.builder)
        self.calls = []

    def buildUnsignedTransaction(self, **kwargs):
        self.calls.append(kwargs)
        forbidden = self.error_on_kwargs.intersection(kwargs)
        if forbidden:
            raise TypeError(f"unsupported {sorted(forbidden)[0]}")
        return "built-wrapper"


class ReputationDataInputsCompatTests(unittest.TestCase):
    def test_build_unsigned_transaction_uses_wrapper_when_supported(self):
        ergo = _FakeErgo(error_on_kwargs=())

        result = transaction._build_unsigned_transaction(
            ergo=ergo,
            input_boxes=["in"],
            outputs=["out"],
            fee=1_000_000,
            sender_address=_FakeSenderAddress(),
            data_inputs=["data-box"],
        )

        self.assertEqual(result, "built-wrapper")
        self.assertIn("dataInput", ergo.calls[0])

    def test_build_unsigned_transaction_falls_back_to_direct_builder(self):
        builder = _FakeBuilder()
        ergo = _FakeErgo(builder=builder)

        result = transaction._build_unsigned_transaction(
            ergo=ergo,
            input_boxes=["in"],
            outputs=["out"],
            fee=1_000_000,
            sender_address=_FakeSenderAddress(),
            data_inputs=["data-box"],
        )

        self.assertEqual(result, "built-directly")
        self.assertEqual(ergo.calls[0]["fee"], 0.001)
        self.assertEqual(builder.calls[-2], ("withDataInputs", ["data-box"]))

    def test_attach_data_inputs_tries_varargs_compatibility(self):
        builder = _FakeBuilder(accept_list=False, accept_add_list=False)

        transaction._attach_data_inputs(builder, ["a", "b"])

        self.assertIn(("addDataInputs", ["a", "b"]), builder.calls)

    def test_explorer_box_to_input_box_uses_output_info_conversion(self):
        fake_output_info_cls = object()
        fake_input_box_impl = mock.Mock(return_value="input-box")
        fake_gson = mock.Mock()
        fake_gson.fromJson.return_value = "output-info"
        fake_json = mock.Mock()
        fake_json.createGson.return_value.create.return_value = fake_gson
        fake_org = types.SimpleNamespace(
            ergoplatform=types.SimpleNamespace(
                explorer=types.SimpleNamespace(
                    client=types.SimpleNamespace(
                        model=types.SimpleNamespace(OutputInfo=fake_output_info_cls)
                    )
                ),
                appkit=types.SimpleNamespace(
                    impl=types.SimpleNamespace(InputBoxImpl=fake_input_box_impl)
                ),
                restapi=types.SimpleNamespace(
                    client=types.SimpleNamespace(JSON=fake_json)
                ),
            )
        )
        fake_jpype = mock.Mock()
        fake_jpype.JPackage.return_value = fake_org

        with mock.patch.object(transaction, "require_java_module", return_value=fake_jpype):
            result = transaction._explorer_box_to_input_box({"boxId": "abc"})

        self.assertEqual(result, "input-box")
        fake_gson.fromJson.assert_called_once()
        self.assertIs(fake_gson.fromJson.call_args[0][1], fake_output_info_cls)
        fake_input_box_impl.assert_called_once_with("output-info")


if __name__ == "__main__":
    unittest.main()
