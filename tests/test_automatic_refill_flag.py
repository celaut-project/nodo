"""`deposits.AUTOMATIC_REFILL: false` must stop the node paying peers on its own.

The manager tick is the only path that signs a payment nobody asked for: a peer's
deposit drops below the threshold and `increase_deposit_on_peer` broadcasts a real
transaction. Until this flag existed the only way to stop it was
`network.DELEGATE_EXECUTION: false`, which also stops delegation -- so an operator
who wanted to keep delegating and approve each payment by hand had nothing to set.

The assertion that matters is `increase_deposit_on_peer` never being called. The
second one -- that the balance is not even queried -- is not decoration:
`balance_on_other_peer` is an RPC that deletes our client on the peer when it fails,
so a tick that cannot act on the answer must not ask the question.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.manager import maintain as maintain_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain_module = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AutomaticRefillFlagTests(unittest.TestCase):

    def _run(self, automatic_refill: bool):
        """Drive the real `peer_deposits` with one reachable, under-funded peer."""
        settings = {
            "network.DELEGATE_EXECUTION": True,
            "deposits.AUTOMATIC_REFILL": automatic_refill,
        }
        connection = mock.MagicMock()
        connection.get_peers_id.return_value = ["peer-1"]
        connection.get_peer_expiry_unix_timestamp.return_value = None
        payments = mock.MagicMock()
        payments.increase_deposit_on_peer.return_value = True

        with mock.patch.object(maintain_module.env_manager, "get",
                               side_effect=lambda key, default=None:
                                   settings.get(key, default)), \
                mock.patch.object(maintain_module, "SQLConnection", return_value=connection), \
                mock.patch.object(maintain_module, "is_peer_available", return_value=True), \
                mock.patch.object(maintain_module, "balance_on_other_peer",
                                  return_value=0) as balance, \
                mock.patch.object(maintain_module, "refill_threshold_mu", return_value=200), \
                mock.patch.object(maintain_module, "full_deposit_mu", return_value=1000), \
                mock.patch.object(maintain_module, "_payment_process_module",
                                  return_value=payments):
            maintain_module.peer_deposits()

        return payments.increase_deposit_on_peer, balance

    def test_an_under_funded_peer_is_topped_up_while_the_flag_is_on(self):
        # Guards the test itself: without it the "off" case could pass for any reason.
        increase, balance = self._run(automatic_refill=True)
        increase.assert_called_once_with(peer_id="peer-1", amount=1000, floor=True)
        balance.assert_called_once()

    def test_nothing_is_paid_or_even_queried_while_the_flag_is_off(self):
        increase, balance = self._run(automatic_refill=False)
        increase.assert_not_called()
        balance.assert_not_called()

    def test_it_says_once_that_it_is_not_funding_anyone(self):
        """Silence reads as broken: an operator who forgot the setting would just watch
        deposits run down. Once, though -- the tick comes round every ten seconds."""
        maintain_module._automatic_refill_announced = False
        with mock.patch.object(maintain_module.log, "LOGGER") as logger:
            self._run(automatic_refill=False)
            self._run(automatic_refill=False)

        announcements = [
            call for call in logger.call_args_list
            if "AUTOMATIC_REFILL is off" in str(call)
        ]
        self.assertEqual(len(announcements), 1, logger.call_args_list)

    def test_turning_it_back_on_arms_the_announcement_again(self):
        maintain_module._automatic_refill_announced = False
        with mock.patch.object(maintain_module.log, "LOGGER") as logger:
            self._run(automatic_refill=False)
            self._run(automatic_refill=True)
            self._run(automatic_refill=False)

        announcements = [
            call for call in logger.call_args_list
            if "AUTOMATIC_REFILL is off" in str(call)
        ]
        self.assertEqual(len(announcements), 2, logger.call_args_list)


if __name__ == "__main__":
    unittest.main()
