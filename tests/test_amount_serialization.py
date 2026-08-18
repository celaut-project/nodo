"""An amount has to survive the round trip to the wire and to a peer refill.

Both bugs covered here were silent: a config value read as a float stringifies to
"1e+64", which ``from_amount`` cannot parse, and the peer-deposit path only
computed the refill amount when DEBUG_MODE was on.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.utils.utils import to_amount, from_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AmountSerializationTests(unittest.TestCase):
    def test_float_valued_amount_round_trips(self):
        # Config values can arrive as floats.
        for value, expected in ((1e64, 10**64), (1.0e9, 10**9), (100.0, 100), ("1e+32", 10**32)):
            with self.subTest(value=value):
                self.assertEqual(from_amount(to_amount(value)), expected)

    def test_unbounded_int_is_not_truncated(self):
        huge = 10**70
        self.assertEqual(from_amount(to_amount(huge)), huge)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerDepositRefillTests(unittest.TestCase):
    def test_underfunded_peer_is_refilled_without_debug_mode(self):
        from src.manager import maintain

        payment_module = unittest.mock.MagicMock()
        payment_module.increase_deposit_on_peer.return_value = True

        with patch.object(maintain.SQLConnection, "get_peers_id", return_value=["peer-1"]), \
             patch.object(maintain, "is_peer_available", return_value=True), \
             patch.object(maintain.SQLConnection, "get_peer_expiry_unix_timestamp", return_value=0), \
             patch.object(maintain, "balance_on_other_peer", return_value=0), \
             patch.object(maintain, "_payment_process_module", return_value=payment_module):
            maintain.peer_deposits(debug_mode=False)

        payment_module.increase_deposit_on_peer.assert_called_once()
        # A peer at zero is topped up to a full deposit, whose size is derived from
        # the ledger's own floor rather than configured.
        self.assertEqual(
            payment_module.increase_deposit_on_peer.call_args.kwargs["amount"],
            maintain.full_deposit_mu(),
        )


if __name__ == "__main__":
    unittest.main()
