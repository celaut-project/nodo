"""`balance_on_other_peer` answers in OUR MU; the row it caches stays in the peer's.

The peer reports what we hold there in its own MU -- MU is an internal unit and two
nodes do not share a scale. Converting at every call site meant `delegate_execution`
did it and `maintain.peer_deposits` did not, so refill sizing compared a peer-scaled
balance against local deposit floors. One conversion, where the figure enters.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.manager import metrics as metrics_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    metrics_mod = None  # type: ignore[assignment]


# One of our MU buys two of the peer's, so its figures halve on the way in.
PAYMENT_SYSTEM = SimpleNamespace(
    local_mu_per_unit=1_000_000_000, peer_mu_per_unit=2_000_000_000
)


def _rates(system=PAYMENT_SYSTEM):
    return patch(
        "src.payment_system.mu_conversion.matching_payment_system",
        return_value=system,
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalanceOnOtherPeerTests(unittest.TestCase):
    def test_the_peers_answer_is_converted_and_cached_unconverted(self):
        metrics = celaut.Metrics()
        metrics.balance.n = "2000000"

        with _rates(), patch.object(
            metrics_mod.sc, "get_peer_by_id", return_value=None
        ), patch.object(
            metrics_mod, "get_client_id_on_other_peer", return_value="client-on-peer"
        ), patch.object(
            metrics_mod, "__get_metrics_external", lambda peer_id, token: metrics
        ), patch.object(
            metrics_mod.sc, "refresh_balance_for_peer", return_value=True
        ) as refresh:
            result = metrics_mod.balance_on_other_peer(peer_id="peer-a")

        self.assertEqual(result, 1_000_000)
        # Cached verbatim: the peer's own accounting, not a figure with our rate
        # baked into it that goes stale the moment either node rescales its MU.
        refresh.assert_called_once_with(peer_id="peer-a", balance_mu=2_000_000)

    def test_the_cached_row_is_converted_on_read_too(self):
        with _rates():
            self.assertEqual(metrics_mod._in_local_mu("peer-a", 2_000_000), 1_000_000)

    def test_rounds_down_so_we_never_overstate_what_we_hold_there(self):
        # 1 of the peer's MU is worth 0.5 of ours; claiming 1 would let a caller
        # commit to a cost the peer will then refuse for lack of balance.
        with _rates():
            self.assertEqual(metrics_mod._in_local_mu("peer-a", 1), 0)

    def test_no_common_payment_system_reads_as_nothing_usable_there(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            side_effect=ValueError("no common payment system"),
        ), patch.object(metrics_mod, "logger"):
            self.assertEqual(metrics_mod._in_local_mu("peer-a", 2_000_000), 0)


if __name__ == "__main__":
    unittest.main()
