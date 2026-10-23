"""What each way of dying does to the reputation of the service (#562).

The father holds the balance of an instance and can empty it on purpose, with small
top-ups. So a death for out of balance is not a fault of the service and scores
nothing. A machine that the service lost is a fault, and it scores the service.

Each test drives the real tick and reads what reaches `update_vmachine_reputation`.
The database and the virtualizer are stubbed: the subject is the rule, not SQLite.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.manager import maintain
from src.reputation_system.reasons import Reason
from src.utils import dead_instances

IDLE = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
SYS_REQ = {
    "mem_limit": 1024 ** 3,
    "disk_space": 10 * 1024 ** 3,
    "cpu_period": 100_000,
    "cpu_quota": 100_000,
}


class InstanceDeathReputationTests(unittest.TestCase):

    def setUp(self):
        maintain._instances_penalised.clear()
        self.reputation = MagicMock()
        self.stop = MagicMock()

    def _local_tick(self, vm_maintain=None, spend_ok=True):
        with patch.object(maintain.sc, "get_all_internal_containers_ids", return_value=["vm-1"]), \
             patch.object(maintain.sc, "get_sys_req", return_value=dict(SYS_REQ)), \
             patch.object(maintain.sc, "record_instance_consumption"), \
             patch.object(maintain, "vm_maintain", side_effect=vm_maintain), \
             patch.object(maintain, "spend_mu", return_value=spend_ok), \
             patch.object(maintain, "stop_instance", self.stop), \
             patch.object(maintain, "system_scarcity", return_value=IDLE), \
             patch.object(maintain, "_reputation_interface", return_value=self.reputation), \
             patch.object(maintain, "vm_janitor_cleanup_orphans"), \
             patch.object(maintain.demand_history, "record_tick"):
            maintain.maintain_vmachines(debug_mode=False)

    def _the_virtualizer_finds_it_dead(self, reason):
        """A `vm_maintain` that reports the instance dead, as the virtualizers do."""
        def vm_maintain(vmachine_id, debug_mode, remove_and_penalize):
            if reason is None:
                remove_and_penalize(vmachine_id=vmachine_id)
            else:
                remove_and_penalize(vmachine_id=vmachine_id, reason=reason)
        return vm_maintain

    def test_out_of_balance_scores_nothing_and_is_recorded(self):
        self._local_tick(spend_ok=False)

        self.reputation.update_vmachine_reputation.assert_not_called()
        self.stop.assert_called_once_with(
            token="vm-1", reason=dead_instances.OUT_OF_BALANCE
        )

    def test_a_lost_machine_lowers_the_reputation(self):
        self._local_tick(vm_maintain=self._the_virtualizer_finds_it_dead(None))

        self.reputation.update_vmachine_reputation.assert_called_once_with(
            vmachine_id="vm-1", amount=-100, reason=Reason.INSTANCE_LOST
        )
        self.stop.assert_called_once_with(
            token="vm-1", reason=dead_instances.PROCESS_LOST
        )

    def test_a_guest_panic_lowers_the_reputation(self):
        self._local_tick(
            vm_maintain=self._the_virtualizer_finds_it_dead(dead_instances.GUEST_PANIC)
        )

        self.reputation.update_vmachine_reputation.assert_called_once_with(
            vmachine_id="vm-1", amount=-100, reason=Reason.INSTANCE_LOST
        )
        self.stop.assert_called_once_with(
            token="vm-1", reason=dead_instances.GUEST_PANIC
        )

    def test_a_charge_that_works_scores_nothing(self):
        self._local_tick(spend_ok=True)

        self.reputation.update_vmachine_reputation.assert_not_called()
        self.stop.assert_not_called()

    def test_a_delegated_instance_out_of_balance_scores_nothing(self):
        """The delegated tick has the same rule as the local tick."""
        row = {
            "peer_id": "peer-1", "token": "token-1", "id": "vm-2",
            "peer_balance_mu": 1000, "balance_mu": 1,
            "mem_limit": 1 << 30, "disk_space": 1 << 30, "cpu_period": 100000,
            "cpu_quota": 100000, "charged_until": 0.0,
        }
        with patch.object(maintain.sc, "get_delegated_instances", return_value=[row]), \
             patch.object(maintain.sc, "settle_delegated_tick"), \
             patch.object(maintain, "_check_our_account"), \
             patch.object(maintain, "_read_child", return_value=(1000, {"peer_balance_mu": 0})), \
             patch.object(maintain, "system_scarcity", return_value={}), \
             patch.object(maintain, "charge_at_launch_price", return_value=500), \
             patch.object(maintain, "ALLOW_DEBT", False), \
             patch.object(maintain, "stop_instance", self.stop), \
             patch.object(maintain, "_reputation_interface", return_value=self.reputation):
            maintain.maintain_delegated_instances()

        self.reputation.update_vmachine_reputation.assert_not_called()
        self.stop.assert_called_once_with(
            token="vm-2", reason=dead_instances.OUT_OF_BALANCE
        )


if __name__ == "__main__":
    unittest.main()
