"""A new client can pay for work without the operator funding it by hand (issue #324).

`costs.ALLOW_DEBT` is off by default, so `spend_mu` refuses any charge a balance cannot
cover. A client created at zero was therefore refused at its first launch: the two
shipped defaults only ever worked together while debt was on.

The credit is one hour of the smallest useful guest -- 0.5 GiB of RAM and one vCPU -- at
this node's own prices. Asserted against those prices rather than against the literal
number, so that changing a price and leaving the credit behind fails here instead of
quietly making the default mean something else.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils.cost_functions.execution_cost import CPU, DISK, MEM, maintenance_charge_mu
    from src.utils.monetary import free_tier
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

GIB = 1024 ** 3
UNLOADED = {CPU: 0.0, MEM: 0.0, DISK: 0.0}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NewClientCreditTests(unittest.TestCase):
    @staticmethod
    def _smallest_guest_hour() -> int:
        """MU for holding 0.5 GiB and one vCPU for an hour, on an unloaded node."""
        return maintenance_charge_mu(
            system_resources=celaut.Sysresources(
                mem_limit=GIB // 2, cpu_period=100000, cpu_quota=100000, disk_space=0
            ),
            seconds=3600,
            scarcity=UNLOADED,
        )

    def test_a_new_client_is_credited_something(self):
        # The reported failure: GenerateClient then StartService, on a default install.
        self.assertGreater(free_tier().credit_mu_per_new_client, 0)

    def test_the_credit_funds_an_hour_of_the_smallest_guest(self):
        self.assertGreaterEqual(
            free_tier().credit_mu_per_new_client, self._smallest_guest_hour()
        )

    def test_the_credit_is_that_hour_and_no_more(self):
        # Pinned in both directions: this is a giveaway, and one that drifted upward
        # would be funding strangers' work out of the operator's capacity.
        self.assertEqual(
            free_tier().credit_mu_per_new_client, self._smallest_guest_hour()
        )


if __name__ == "__main__":
    unittest.main()
