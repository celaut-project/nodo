"""StopService and ModifyDeposit need the client that is the father of the instance (#597).

While the father of an instance is a client, the token alone does not end the instance or
move its money: the request carries that client too, and a client that is not the father
is refused whatever token it holds. A father that is not a client leaves the token as the
only credential. GetMetrics answers for an instance token only, and the balance of a
client has an RPC of its own.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2
    from protos.gateway_bee import GATEWAY_RPCS
    from src.gateway import client_gate
    from src.gateway import gateway as gateway_module
    from src.manager import metrics as metrics_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

TOKEN = "instance-token"
FATHER = "client-father"
OTHER = "client-other"


class _Context:
    def peer(self):
        return "ipv4:9.9.9.9:4442"

    def abort(self, code, details):  # pragma: no cover - nothing here aborts
        raise AssertionError(f"{code}: {details}")


def _caller_is(identity):
    """``require_caller`` for a request whose client_id is ``identity``."""
    def require_caller(context, client_id=""):
        if client_id != identity:
            raise client_gate.ClientRequired(f"no such client {client_id!r}")
        return identity
    return require_caller


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RequireFatherTests(unittest.TestCase):

    def check(self, *, father, father_is_client, caller_client_id):
        with patch.object(client_gate.sc, "father_of", return_value=father), \
                patch.object(client_gate.sc, "client_exists",
                             side_effect=lambda client_id: father_is_client and client_id == father), \
                patch.object(client_gate, "require_caller",
                             side_effect=_caller_is(caller_client_id)) as require_caller:
            client_gate.require_father(_Context(), TOKEN, caller_client_id)
        return require_caller

    def test_the_father_that_is_a_client_may_call(self):
        self.check(father=FATHER, father_is_client=True, caller_client_id=FATHER)

    def test_another_client_is_refused_even_though_it_is_a_valid_client(self):
        with self.assertRaises(client_gate.ClientRequired):
            self.check(father=FATHER, father_is_client=True, caller_client_id=OTHER)

    def test_no_client_is_refused_when_the_father_is_a_client(self):
        with patch.object(client_gate.sc, "father_of", return_value=FATHER), \
                patch.object(client_gate.sc, "client_exists", return_value=True), \
                patch.object(client_gate, "require_caller",
                             side_effect=client_gate.ClientRequired("missing")):
            with self.assertRaises(client_gate.ClientRequired):
                client_gate.require_father(_Context(), TOKEN, "")

    def test_a_local_instance_is_not_the_client_that_is_the_father(self):
        # require_caller lets a local instance through with its own token as identity.
        with patch.object(client_gate.sc, "father_of", return_value=FATHER), \
                patch.object(client_gate.sc, "client_exists", return_value=True), \
                patch.object(client_gate, "require_caller", return_value="some-instance"):
            with self.assertRaises(client_gate.ClientRequired):
                client_gate.require_father(_Context(), TOKEN, "")

    def test_a_father_that_is_not_a_client_leaves_the_token_as_the_credential(self):
        require_caller = self.check(father="father-instance", father_is_client=False,
                                    caller_client_id="")
        require_caller.assert_not_called()

    def test_a_token_that_this_node_does_not_know_passes_to_the_call(self):
        require_caller = self.check(father=None, father_is_client=False, caller_client_id="")
        require_caller.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FatherGatedGatewayTests(unittest.TestCase):

    def setUp(self):
        self.gateway = gateway_module.Gateway()
        # The father of TOKEN is the client FATHER.
        for target, kwargs in (
            (client_gate.sc, {"father_of": {"return_value": FATHER}}),
            (client_gate.sc, {"client_exists": {"side_effect": lambda client_id: client_id in (FATHER, OTHER)}}),
        ):
            for name, options in kwargs.items():
                patcher = patch.object(target, name, **options)
                patcher.start()
                self.addCleanup(patcher.stop)

    def stop(self, client_id):
        request = (celaut_pb2.TokenMessage(token=TOKEN), client_id)
        with patch.object(gateway_module, "parse_with_client", return_value=request), \
                patch.object(client_gate, "require_caller", side_effect=_caller_is(client_id)), \
                patch.object(gateway_module, "stop_instance", return_value=7) as stop_instance, \
                patch.object(gateway_module.BeeClient, "respond",
                             side_effect=lambda message_iterator: iter([message_iterator])):
            try:
                return list(self.gateway.StopService(iter(()), _Context())), stop_instance
            except client_gate.ClientRequired as e:
                return e, stop_instance

    def modify(self, client_id, difference):
        amount = celaut_pb2.Amount(n=str(difference))
        request = (celaut_pb2.ModifyDepositInput(difference=amount, service_token=TOKEN), client_id)
        with patch.object(gateway_module, "parse_with_client", return_value=request), \
                patch.object(client_gate, "require_caller", side_effect=_caller_is(client_id)), \
                patch.object(gateway_module, "modify_deposit", return_value=(True, "ok")) as modify, \
                patch.object(gateway_module.BeeClient, "respond",
                             side_effect=lambda message_iterator: iter([message_iterator])):
            try:
                return list(self.gateway.ModifyDeposit(iter(()), _Context())), modify
            except client_gate.ClientRequired as e:
                return e, modify

    def test_the_father_stops_its_instance(self):
        result, stop_instance = self.stop(FATHER)

        stop_instance.assert_called_once_with(token=TOKEN)
        self.assertEqual(len(result), 1)

    def test_another_client_does_not_stop_it_with_the_token(self):
        error, stop_instance = self.stop(OTHER)

        self.assertIsInstance(error, client_gate.ClientRequired)
        stop_instance.assert_not_called()

    def test_no_client_does_not_stop_it_with_the_token(self):
        error, stop_instance = self.stop("")

        self.assertIsInstance(error, client_gate.ClientRequired)
        stop_instance.assert_not_called()

    def test_the_father_tops_up_and_withdraws(self):
        for difference in (500, -500):
            with self.subTest(difference=difference):
                result, modify = self.modify(FATHER, difference)
                modify.assert_called_once_with(amount_mu=difference, service_token=TOKEN)
                self.assertEqual(len(result), 1)

    def test_another_client_moves_no_money_with_the_token_in_either_direction(self):
        for difference in (500, -500):
            with self.subTest(difference=difference):
                error, modify = self.modify(OTHER, difference)
                self.assertIsInstance(error, client_gate.ClientRequired)
                modify.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MetricsSplitTests(unittest.TestCase):

    def test_get_metrics_does_not_answer_for_a_client_id(self):
        with patch.object(metrics_mod.sc, "internal_instance_exists", return_value=False), \
                patch.object(metrics_mod.sc, "get_delegated_token_by_id", return_value=None), \
                patch.object(metrics_mod.sc, "client_exists", return_value=True), \
                patch.object(metrics_mod.sc, "get_client_balance", return_value=(5, 0, 0)) as balance:
            with self.assertRaises(Exception):
                metrics_mod.get_metrics(token=FATHER)
        balance.assert_not_called()

    def test_get_client_metrics_answers_with_the_balance_of_the_client(self):
        with patch.object(metrics_mod.sc, "get_client_balance", return_value=(5, 0, 0)):
            self.assertEqual(metrics_mod.get_client_metrics(FATHER).balance.n, "5")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetClientMetricsGatewayTests(unittest.TestCase):

    def read(self, sent_client_id, identified_as):
        with patch.object(gateway_module.BeeClient, "parse_one",
                          return_value=celaut_pb2.Client(client_id=sent_client_id)), \
                patch.object(gateway_module, "require_caller", return_value=identified_as), \
                patch.object(gateway_module, "get_client_metrics",
                             return_value=celaut_pb2.Metrics()) as read, \
                patch.object(gateway_module.BeeClient, "respond",
                             side_effect=lambda message_iterator, indices=None: iter([message_iterator])):
            try:
                return list(gateway_module.Gateway().GetClientMetrics(iter(()), _Context())), read
            except client_gate.ClientRequired as e:
                return e, read

    def test_a_client_reads_its_own_balance(self):
        result, read = self.read(FATHER, FATHER)

        read.assert_called_once_with(client_id=FATHER)
        self.assertEqual(len(result), 1)

    def test_a_local_instance_has_no_client_balance_to_read(self):
        error, read = self.read(FATHER, "some-instance")

        self.assertIsInstance(error, client_gate.ClientRequired)
        read.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RpcTableTests(unittest.TestCase):

    def test_the_token_and_client_rpcs_take_the_client_at_two(self):
        for name in ("StopService", "ModifyDeposit"):
            with self.subTest(rpc=name):
                rpc = GATEWAY_RPCS[name]
                self.assertEqual(rpc.auth, "token+client")
                self.assertIs(rpc.input[2], celaut_pb2.Client)

    def test_get_metrics_takes_only_a_token_and_client_metrics_only_a_client(self):
        self.assertEqual(GATEWAY_RPCS["GetMetrics"].input, {1: celaut_pb2.TokenMessage})
        self.assertEqual(GATEWAY_RPCS["GetClientMetrics"].input, {1: celaut_pb2.Client})
        self.assertEqual(GATEWAY_RPCS["GetClientMetrics"].auth, "client")


if __name__ == "__main__":
    unittest.main()
