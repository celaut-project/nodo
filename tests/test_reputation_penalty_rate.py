"""Reputation moves on events, not on how often the manager looks.

The maintenance tick runs every MANAGER_ITERATION_TIME -- ten seconds by default --
over every peer and every instance, and the penalties it applies used to repeat on
each pass. A peer someone unplugged for a day lost some 864 000 points, which records
nothing about the peer: it counts how many times we noticed. The instance penalties
have the same shape whenever pruning fails and the row survives into the next sweep.

So each of them fires once per episode: on the way down for a peer (armed again when
it answers), and once per instance per outcome.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.manager import maintain as maintain_module
    from src.reputation_system.reasons import Reason
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain_module = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UnreachablePeerPenaltyTests(unittest.TestCase):

    def setUp(self):
        maintain_module._peers_penalised_for_refresh.clear()
        self.reputation = mock.MagicMock()

    def _tick(self, reachable: bool, peers=("peer-1",)):
        """One `peer_deposits` pass with the peer reachable or not.

        Refills are switched off so the tick stops after the refresh: what is under
        test is the penalty, not the deposit sizing below it.
        """
        settings = {
            "network.DELEGATE_EXECUTION": True,
            "deposits.AUTOMATIC_REFILL": False,
        }
        connection = mock.MagicMock()
        connection.get_peers_id.return_value = list(peers)
        connection.get_peer_expiry_unix_timestamp.return_value = None

        with mock.patch.object(maintain_module.env_manager, "get",
                               side_effect=lambda key, default=None: settings.get(key, default)), \
                mock.patch.object(maintain_module, "SQLConnection", return_value=connection), \
                mock.patch.object(maintain_module, "is_peer_available", return_value=reachable), \
                mock.patch.object(maintain_module, "beerpc") as beerpc, \
                mock.patch.object(maintain_module, "accept_peer_refresh", return_value=True), \
                mock.patch.object(maintain_module, "_reputation_interface",
                                  return_value=self.reputation):
            # Unreachable: the refresh call is what fails.
            beerpc.client_grpc.side_effect = (
                Exception("no route to peer") if not reachable else None
            )
            maintain_module.peer_deposits()

    def _penalties(self):
        return [
            call for call in self.reputation.update_peer_reputation.call_args_list
            if call.kwargs.get("reason") == Reason.PEER_REFRESH_FAILED
        ]

    def test_an_outage_costs_one_penalty_however_long_it_lasts(self):
        for _ in range(5):
            self._tick(reachable=False)

        self.assertEqual(len(self._penalties()), 1, self._penalties())
        self.assertEqual(self._penalties()[0].kwargs["amount"], -100)

    def test_a_peer_that_never_failed_is_never_penalised(self):
        # Guards the test itself: without it the case above could pass for any reason.
        for _ in range(3):
            self._tick(reachable=True)

        self.assertEqual(self._penalties(), [])

    def test_a_second_outage_is_a_second_event(self):
        self._tick(reachable=False)
        self._tick(reachable=True)   # recovered: the penalty is armed again
        self._tick(reachable=False)

        self.assertEqual(len(self._penalties()), 2, self._penalties())

    def test_a_peer_that_is_forgotten_leaves_nothing_behind(self):
        """The set is in memory, so it must not accumulate peers that no longer exist."""
        self._tick(reachable=False)
        self.assertIn("peer-1", maintain_module._peers_penalised_for_refresh)

        self._tick(reachable=False, peers=("peer-2",))

        self.assertNotIn("peer-1", maintain_module._peers_penalised_for_refresh)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InstancePenaltyTests(unittest.TestCase):
    """Both instance penalties are meant to end with the instance pruned. Pruning can
    fail -- a virtualizer that will not let go leaves the row in place -- and the next
    sweep would score the same loss again."""

    def setUp(self):
        maintain_module._instances_penalised.clear()
        self.reputation = mock.MagicMock()
        patch = mock.patch.object(maintain_module, "_reputation_interface",
                                  return_value=self.reputation)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_instance_is_scored_once_for_the_same_outcome(self):
        for _ in range(4):
            maintain_module._penalise_instance_once(
                "instance-1", -100, Reason.INSTANCE_LOST
            )

        self.reputation.update_vmachine_reputation.assert_called_once_with(
            vmachine_id="instance-1", amount=-100, reason=Reason.INSTANCE_LOST
        )

    def test_a_different_outcome_is_a_different_event(self):
        maintain_module._penalise_instance_once("instance-1", -100, Reason.INSTANCE_LOST)
        maintain_module._penalise_instance_once(
            "instance-1", -10, Reason.INSTANCE_OUT_OF_BALANCE
        )

        self.assertEqual(self.reputation.update_vmachine_reputation.call_count, 2)

    def test_a_sweep_forgets_instances_that_are_gone(self):
        """The set is in memory; it must track what is running, not what ever ran."""
        maintain_module._penalise_instance_once("instance-1", -100, Reason.INSTANCE_LOST)
        self.assertTrue(maintain_module._instances_penalised)

        with mock.patch.object(maintain_module.sc, "get_all_internal_containers_ids",
                               return_value=[]), \
                mock.patch.object(maintain_module, "system_scarcity", return_value={}):
            maintain_module.maintain_vmachines()

        self.assertFalse(maintain_module._instances_penalised)


if __name__ == "__main__":
    unittest.main()
