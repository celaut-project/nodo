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
