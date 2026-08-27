import unittest
from unittest.mock import patch

from protos import celaut_pb2
from src.manager.modify_resources import modify_sysreq
from src.manager.resources import IOBigData
import src.manager.manager as manager_module


class ModifySysreqMemoryAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        IOBigData._instances.pop(IOBigData, None)

    def tearDown(self) -> None:
        IOBigData._instances.pop(IOBigData, None)

    def test_modify_sysreq_releases_memory_when_limit_decreases(self):
        io_big_data = IOBigData(ram_pool_method=lambda: 1024**3)
        io_big_data.ram_locked = 200

        sys_req = celaut_pb2.Sysresources(mem_limit=50)

        with patch("src.manager.modify_resources.SQLConnection") as mock_sql_connection:
            mock_sc = mock_sql_connection.return_value
            mock_sc.internal_instance_exists.return_value = True
            mock_sc.get_sys_req.return_value = {"mem_limit": 100}
            mock_sc.update_sys_req.return_value = True

            self.assertTrue(modify_sysreq(id="vm-1", sys_req=sys_req))

        self.assertEqual(io_big_data.ram_locked, 150)


class ModifySysreqGrowthOnlyTests(unittest.TestCase):
    """A resize may only be refused for the memory it actually asks to add.

    The instance already holds its current limit. Asking the pool about the new
    *absolute* limit counted those bytes twice, so on a busy host a request that
    added nothing -- re-stating the current ceiling, or releasing memory -- was
    answered with "Insufficient memory." and the caller got
    `Exception on service modify method.`
    """

    ROW = {"mem_limit": 67108864, "disk_space": 0, "cpu_period": 0, "cpu_quota": 0}

    def _modify(self, target_mem, pool_answer):
        with patch("src.manager.modify_resources.SQLConnection") as mock_sql_connection, \
                patch.object(IOBigData, "prevent_kill", return_value=pool_answer) as prevent_kill:
            mock_sc = mock_sql_connection.return_value
            mock_sc.internal_instance_exists.return_value = True
            mock_sc.get_sys_req.return_value = dict(self.ROW)
            mock_sc.update_sys_req.return_value = True
            result = modify_sysreq(
                id="vm-1", sys_req=celaut_pb2.Sysresources(mem_limit=target_mem)
            )
        return result, prevent_kill

    def test_reaffirming_the_current_limit_never_consults_the_pool(self):
        # The live failure: node_controller's modify_resources re-states the ceiling
        # it already holds to read back a balance.
        result, prevent_kill = self._modify(self.ROW["mem_limit"], pool_answer=False)
        self.assertTrue(result)
        prevent_kill.assert_not_called()

    def test_releasing_memory_is_never_refused_for_lack_of_memory(self):
        result, prevent_kill = self._modify(self.ROW["mem_limit"] // 2, pool_answer=False)
        self.assertTrue(result)
        prevent_kill.assert_not_called()

    def test_growth_is_checked_against_the_delta_not_the_absolute_limit(self):
        target = self.ROW["mem_limit"] + 1024
        result, prevent_kill = self._modify(target, pool_answer=True)
        self.assertTrue(result)
        prevent_kill.assert_called_once_with(len=1024)

    def test_growth_that_does_not_fit_is_still_refused(self):
        result, prevent_kill = self._modify(self.ROW["mem_limit"] + 1024, pool_answer=False)
        self.assertFalse(result)
        prevent_kill.assert_called_once_with(len=1024)


class StopInstanceMemoryAccountingTests(unittest.TestCase):
    def test_stop_instance_releases_reserved_memory_for_internal_instances(self):
        with patch.object(manager_module.sc, "internal_instance_exists", side_effect=lambda id: id == "vm-1"), patch.object(
            manager_module.sc, "get_sys_req", return_value={"mem_limit": 256}
        ), patch.object(
            manager_module.sc, "get_internal_father_id", return_value=""
        ), patch.object(
            manager_module.sc, "get_internal_instance", return_value=None
        ), patch.object(
            manager_module.sc, "get_instance_balance", return_value=123
        ), patch.object(
            manager_module.sc, "purge_internal", return_value=None
        ), patch.object(
            manager_module, "kill", return_value=True
        ), patch.object(
            manager_module.IOBigData(), "unlock_ram"
        ) as mock_unlock:
            refund = manager_module.stop_instance(token="vm-1")

        self.assertEqual(refund, 123)
        mock_unlock.assert_called_once_with(ram_amount=256)


if __name__ == "__main__":
    unittest.main()
