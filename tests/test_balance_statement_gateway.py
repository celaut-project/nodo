"""GetBalanceStatement through the gateway (#570): who may read what, and how a refusal
says why.

The caller tells "you cannot pay for this" from "this is not yours" from a fault of the
node by the status code, and only the last says anything about the node.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    import grpc

    from protos import celaut_pb2
    from src.database import ledger
    from src.gateway import gateway as gateway_module
    from src.manager import balance_statement
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Aborted(Exception):
    def __init__(self, code, details):
        super().__init__(details)
        self.code, self.details = code, details


class _Context:
    """The part of grpc's ServicerContext that a handler uses to refuse."""

    def peer(self):
        return "ipv4:9.9.9.9:4442"

    def abort(self, code, details):
        raise _Aborted(code, details)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetBalanceStatementGatewayTests(unittest.TestCase):

    def setUp(self):
        self.gateway = gateway_module.Gateway()
        self.request = celaut_pb2.BalanceStatementRequest(token="child", from_seq=3, to_seq=9)
        patcher = patch.object(
            gateway_module, "parse_with_client", return_value=(self.request, "client-1"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(gateway_module, "require_caller", return_value="client-1")
        self.require_caller = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, **answer):
        with patch.object(balance_statement, "answer", **answer) as mocked:
            try:
                return list(self.gateway.GetBalanceStatement(iter(()), _Context())), mocked
            except _Aborted as e:
                return e, mocked

    def test_the_caller_and_the_range_reach_the_answer(self):
        statement = celaut_pb2.BalanceStatement(head_seq=4)
        with patch.object(gateway_module.BeeClient, "respond",
                          side_effect=lambda message: iter([message])):
            result, mocked = self.call(return_value=statement)

        self.assertEqual(result, [statement])
        mocked.assert_called_once()
        self.assertEqual(
            (mocked.call_args.kwargs["caller"], mocked.call_args.kwargs["token"],
             mocked.call_args.kwargs["from_seq"], mocked.call_args.kwargs["to_seq"]),
            ("client-1", "child", 3, 9),
        )

    def test_a_caller_that_cannot_pay_gets_resource_exhausted(self):
        error, _ = self.call(side_effect=ledger.InsufficientBalance("holds 1, needed 5"))

        self.assertEqual(error.code, grpc.StatusCode.RESOURCE_EXHAUSTED)

    def test_an_account_that_is_not_the_callers_gets_permission_denied(self):
        error, _ = self.call(side_effect=balance_statement.StatementRefused("not yours"))

        self.assertEqual(error.code, grpc.StatusCode.PERMISSION_DENIED)

    def test_an_account_that_does_not_exist_gets_not_found(self):
        error, _ = self.call(side_effect=ledger.NoSuchAccount("child"))

        self.assertEqual(error.code, grpc.StatusCode.NOT_FOUND)

    def test_a_delegated_instance_gets_unimplemented(self):
        error, _ = self.call(side_effect=balance_statement.StatementUnavailable("delegated"))

        self.assertEqual(error.code, grpc.StatusCode.UNIMPLEMENTED)

    def test_a_fault_of_the_node_is_internal_and_does_not_leak_its_cause(self):
        error, _ = self.call(side_effect=RuntimeError("secret detail of the database"))

        self.assertEqual(error.code, grpc.StatusCode.INTERNAL)
        self.assertNotIn("secret", error.details)


if __name__ == "__main__":
    unittest.main()
