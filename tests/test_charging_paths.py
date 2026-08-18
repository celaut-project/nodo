"""The paths that actually move money, exercised end to end.

Every one of these covers a call that a rename had silently broken: the arguments no
longer matched the function, so the call raised TypeError the first time it ran. None
of it showed up in the existing suite, because these paths had tests for what they
decide but never for the call itself, and because two of the four failures were
swallowed by a bare `except`.

They deliberately stop short of the database and the ledger -- what is under test is
that the charging call is well-formed and lands with the right amount, not what SQLite
or Ergo do with it afterwards.

The imports below are deliberately NOT guarded by a try/skipIf: a missing dependency has
to turn this file red. Reporting OK because nothing ran is the worse failure on a path
that moves money.
"""

import unittest
from unittest.mock import MagicMock, patch

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost


PRICES = {
    "pricing.RAM_MU_PER_GIB_HOUR": 1_000_000,
    "pricing.CPU_MU_PER_VCPU_HOUR": 4_000_000,
    "pricing.DISK_MU_PER_GIB_HOUR": 100_000,
    "pricing.SCARCITY_MAX_MULTIPLIER": 1,
    "pricing.SCARCITY_CURVE": 1.0,
    "free_tier.FREE_WHILE_SCARCITY_BELOW": 0.0,
    "ledgers.ergo.payments.MU_PER_NANOERG": 1,
    "ui.DISPLAY_UNIT": "erg",
}

IDLE = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}


def _config(**overrides):
    values = dict(PRICES)
    values.update(overrides)
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


class MaintenanceTickTests(unittest.TestCase):
    """The manager's per-iteration charge, the node's main revenue path."""

    def test_a_sweep_can_price_every_instance_against_one_load_reading(self):
        """`maintain_vmachines` samples system load once and passes it down.

        The parameter did not exist, so every tick raised TypeError -- and nothing
        guards the call, so the manager thread died on the first instance.
        """
        with _config():
            charge = compute_maintenance_cost(
                system_resources=celaut.Sysresources(mem_limit=1024 ** 3),
                seconds=3600,
                scarcity=IDLE,
            )
        self.assertEqual(charge, 1_000_000)

    def test_the_shared_reading_is_what_prices_the_instance(self):
        """Not merely accepted and ignored: a scarce reading must cost more."""
        resources = celaut.Sysresources(mem_limit=1024 ** 3)
        with _config(**{"pricing.SCARCITY_MAX_MULTIPLIER": 4}):
            idle = compute_maintenance_cost(
                system_resources=resources, seconds=3600, scarcity=IDLE
            )
            scarce = compute_maintenance_cost(
                system_resources=resources,
                seconds=3600,
                scarcity={"cpu": 0.0, "mem": 1.0, "disk": 0.0},
            )
        self.assertEqual(scarce, idle * 4)

    # What one instance row says it holds: 1 GiB of memory, 1 vCPU, 10 GiB of disk.
    SYS_REQ = {
        "mem_limit": 1024 ** 3,
        "disk_space": 10 * 1024 ** 3,
        "cpu_period": 100_000,
        "cpu_quota": 100_000,
    }

    def _charge_for(self, sys_req: dict) -> int:
        """Run one manager sweep over a single instance and return what it charged."""
        from src.manager import maintain

        spend = MagicMock(return_value=True)
        # Recording the burn-rate sample is a database write; like `spend_mu` it is
        # stubbed so this stays a test of the charge, not of SQLite. The sample and the
        # charge must not diverge, so it is asserted to land with the same amount below.
        record = MagicMock()
        with _config(), \
             patch.object(maintain.sc, "get_all_internal_containers_ids", return_value=["vm-1"]), \
             patch.object(maintain.sc, "get_sys_req", return_value=sys_req), \
             patch.object(maintain.sc, "record_instance_consumption", record), \
             patch.object(maintain, "vm_maintain"), \
             patch.object(maintain, "spend_mu", spend), \
             patch.object(maintain, "system_scarcity", return_value=IDLE), \
             patch.object(maintain, "_reputation_interface"), \
             patch("src.virtualizers.ch.maintain.janitor_cleanup_orphans"):
            maintain.maintain_vmachines(debug_mode=False)

        spend.assert_called_once()
        self.assertEqual(spend.call_args.kwargs["id"], "vm-1")
        amount_mu = spend.call_args.kwargs["amount_mu"]
        # A successful charge records exactly one burn-rate sample, for the same
        # instance, of the same amount, over the interval that was just billed.
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["id"], "vm-1")
        self.assertEqual(record.call_args.kwargs["charge_mu"], amount_mu)
        self.assertEqual(record.call_args.kwargs["seconds"], maintain.MANAGER_ITERATION_TIME)
        return amount_mu

    def test_the_manager_charges_each_running_instance(self):
        """The whole tick, from the instance list to the deduction."""
        from src.manager import maintain

        seconds = maintain.MANAGER_ITERATION_TIME
        # Per resource, each truncated on its own, the way maintenance_charge_mu sums it.
        expected = (
            1_000_000 * seconds // 3600            # 1 GiB of memory
            + 4_000_000 * seconds // 3600          # 1 vCPU
            + 100_000 * 10 * seconds // 3600       # 10 GiB of disk
        )
        self.assertEqual(self._charge_for(dict(self.SYS_REQ)), expected)

    def test_the_tick_bills_the_cpu_the_row_records(self):
        """The tick built a Sysresources out of memory and disk only.

        `requested_units` then read no CFS pair and priced the instance at zero vCPUs, so
        `pricing.CPU_MU_PER_VCPU_HOUR` was never charged on the one path that bills
        anybody -- the same shape as the `cpu_limit` field that never existed.
        """
        with_cpu = self._charge_for(dict(self.SYS_REQ))
        without_cpu = self._charge_for({**self.SYS_REQ, "cpu_period": 0, "cpu_quota": 0})

        from src.manager import maintain

        self.assertEqual(
            with_cpu - without_cpu,
            4_000_000 * maintain.MANAGER_ITERATION_TIME // 3600,
        )


class InstanceDeductionTests(unittest.TestCase):
    """`autospec=True` throughout, deliberately.

    A plain MagicMock accepts any keyword at all, so it would have happily absorbed the
    very argument mismatch these tests exist to catch. Autospec makes the stub enforce
    the real signature.
    """

    def test_spend_mu_reaches_the_database_accessor(self):
        """`spend_mu` -> `spend_instance_balance` was passing an argument the
        accessor did not have, so charging any instance raised TypeError."""
        from src.manager import manager

        with _config(), \
             patch.object(manager.sc, "client_exists", return_value=False), \
             patch.object(manager.sc, "internal_instance_exists", return_value=True), \
             patch.object(manager.sc, "spend_instance_balance", autospec=True, return_value=True) as spend:
            self.assertTrue(manager.spend_mu(id="vm-1", amount_mu=4_242, debug_mode=False))

        spend.assert_called_once_with(id="vm-1", amount_mu=4_242, allow_debt=manager.ALLOW_DEBT)

    def test_a_client_is_charged_through_the_same_call(self):
        from src.manager import manager

        with _config(), \
             patch.object(manager.sc, "client_exists", return_value=True), \
             patch.object(manager.sc, "get_client_balance", return_value=(10_000, None, "")), \
             patch.object(manager.sc, "reduce_balance", autospec=True) as reduce_balance:
            self.assertTrue(manager.spend_mu(id="client-1", amount_mu=4_242, debug_mode=False))

        reduce_balance.assert_called_once_with(client_id="client-1", balance_mu=4_242)


class PaymentCreditTests(unittest.TestCase):
    def test_a_validated_payment_credits_the_client(self):
        """This one failed silently rather than loudly.

        `validate_payment_process` wraps the credit in a bare `except`, so a broken
        call was swallowed and the deposit was marked "rejected" -- while the funds had
        already arrived on-chain and the client was credited nothing. The assertion
        that matters is therefore both halves: the credit call is well-formed, AND the
        deposit ends up marked paid.
        """
        from src.payment_system import payment_process

        manager_module = MagicMock()
        manager_module.increase_local_balance_for_client.return_value = True

        with patch.object(payment_process.sc, "deposit_token_exists", return_value=True), \
             patch.object(payment_process.sc, "client_id_from_deposit_token", return_value="client-1"), \
             patch.object(payment_process.sc, "update_deposit_token") as update_token, \
             patch.object(payment_process.sc, "get_deposit_tokens", return_value=[]), \
             patch.object(payment_process, "_manager_module", return_value=manager_module), \
             patch.object(payment_process, "__check_payment_process", return_value=True):
            credited = payment_process.validate_payment_process(
                amount=5_000,
                ledger=celaut.Contract.Ledger(),
                contract=b"contract",
                script=b"script",
                token="deposit-token",
            )

        manager_module.increase_local_balance_for_client.assert_called_once_with(
            client_id="client-1", amount_mu=5_000
        )
        self.assertTrue(credited)
        update_token.assert_called_once_with(token_id="deposit-token", status="payed")


if __name__ == "__main__":
    unittest.main()
