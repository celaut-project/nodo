"""`get_metrics` answers for an instance this node delegated on (#442).

`delegate_execution` hands the father the sha256 hex of the peer's token as the
child's token. When that father is itself a node, its maintenance tick reads the
child's balance back with exactly that alias; `get_metrics` only looked delegated
instances up for tokens containing '##', so every such read failed with "Invalid
token" and the father stopped a child that was running fine.
"""
import unittest
from hashlib import sha256
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.manager import metrics as metrics_mod
    from src.utils.utils import from_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    metrics_mod = None  # type: ignore[assignment]


PEER_TOKEN = "token-as-the-next-peer-knows-it"
ALIAS = sha256(PEER_TOKEN.encode("utf-8")).hexdigest()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatedAliasMetricsTests(unittest.TestCase):

    def _sc(self, delegated_token):
        sc = patch.object(metrics_mod, "sc").start()
        self.addCleanup(patch.stopall)
        sc.client_exists.return_value = False
        sc.internal_instance_exists.return_value = False
        sc.get_delegated_token_by_id.side_effect = (
            lambda id: delegated_token if id == ALIAS else None
        )
        sc.get_delegated_balance.return_value = 123_456
        return sc

    def test_the_alias_handed_out_on_delegation_is_answered(self):
        sc = self._sc(delegated_token=PEER_TOKEN)

        metrics = metrics_mod.get_metrics(token=ALIAS)

        self.assertEqual(from_amount(metrics.balance), 123_456)
        sc.get_delegated_balance.assert_called_once_with(token=PEER_TOKEN)

    def test_an_unknown_token_is_still_refused(self):
        self._sc(delegated_token=None)

        with self.assertRaisesRegex(Exception, "Invalid token.*deadbeef"):
            metrics_mod.get_metrics(token="deadbeef")


if __name__ == "__main__":
    unittest.main()
