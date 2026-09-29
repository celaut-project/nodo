"""`deposits.AUTOMATIC_REFILL_GOSSIP_PEERS` decides whether gossip-learned peers are funded.

Issue #427: a peer registered from a third party's gossip is not one this node chose,
and keypairs are free, so by default the automatic refill skips it. The flag lets an
operator opt back in. It only narrows what `deposits.AUTOMATIC_REFILL` already allows:
with the automatic refill off, nobody is funded whatever this flag says. Anything but
a real `true` reads as off.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.manager import maintain as maintain_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain_module = None  # type: ignore[assignment]

_UNSET = object()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AutomaticRefillGossipPeersFlagTests(unittest.TestCase):

    def _funded(self, gossip_peers=_UNSET, automatic_refill=True):
        """Run the real `peer_deposits` over a chosen peer and a gossip-learned one,
        both reachable and under-funded; return the ids that were paid."""
        settings = {
            "network.DELEGATE_EXECUTION": True,
            "deposits.AUTOMATIC_REFILL": automatic_refill,
        }
        if gossip_peers is not _UNSET:
            settings["deposits.AUTOMATIC_REFILL_GOSSIP_PEERS"] = gossip_peers
        connection = mock.MagicMock()
        connection.get_peers_id.return_value = ["connected", "gossiped"]
        connection.get_peer_expiry_unix_timestamp.return_value = None
        connection.peer_learned_via_gossip.side_effect = \
            lambda peer_id: peer_id == "gossiped"
        payments = mock.MagicMock()
        payments.increase_deposit_on_peer.return_value = True

        with mock.patch.object(maintain_module.env_manager, "get",
                               side_effect=lambda key, default=None:
                                   settings.get(key, default)), \
                mock.patch.object(maintain_module, "SQLConnection", return_value=connection), \
                mock.patch.object(maintain_module, "is_peer_available", return_value=True), \
                mock.patch.object(maintain_module, "balance_on_other_peer", return_value=0), \
                mock.patch.object(maintain_module, "refill_threshold_mu", return_value=200), \
                mock.patch.object(maintain_module, "full_deposit_mu", return_value=1000), \
                mock.patch.object(maintain_module, "matching_payment_system",
                                  return_value=mock.MagicMock()), \
                mock.patch.object(maintain_module, "_payment_process_module",
                                  return_value=payments):
            maintain_module.peer_deposits()

        return sorted(call.kwargs["peer_id"]
                      for call in payments.increase_deposit_on_peer.call_args_list)

    def test_by_default_only_the_connected_peer_is_refilled(self):
        self.assertEqual(self._funded(), ["connected"])

    def test_flag_false_skips_the_gossip_peer(self):
        self.assertEqual(self._funded(gossip_peers=False), ["connected"])

    def test_flag_true_refills_both(self):
        self.assertEqual(self._funded(gossip_peers=True), ["connected", "gossiped"])

    def test_flag_true_never_enables_refill_by_itself(self):
        self.assertEqual(self._funded(gossip_peers=True, automatic_refill=False), [])

    def test_an_invalid_value_is_treated_as_false(self):
        for invalid in ("true", "yes", 1, None, [], {"on": True}):
            with self.subTest(value=invalid):
                self.assertEqual(self._funded(gossip_peers=invalid), ["connected"])


if __name__ == "__main__":
    unittest.main()
