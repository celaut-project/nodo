"""``nodo hibernate``: the operator puts an instance in hibernation (TUI `h`)."""
import unittest
from unittest.mock import MagicMock, patch

from src.commands import hibernate as command
from src.database import ledger
from src.manager import hibernation, stopping


class HibernateCommandTests(unittest.TestCase):
    def _run(self, *, exists=True, hibernated=False, admin=True, request=True):
        sc = MagicMock()
        sc.internal_instance_exists.return_value = exists
        sc.is_local_instance_hibernated.return_value = hibernated
        with patch.object(command, "sc", sc), \
                patch.object(command, "resolve_instance_token", return_value="tok"), \
                patch.object(command, "can_admin_network", return_value=admin), \
                patch.object(stopping, "request_hibernation", side_effect=request
                             if isinstance(request, Exception) else None,
                             return_value=request if not isinstance(request, Exception) else None):
            ok = command.hibernate("name")
        return ok, sc

    def test_the_row_says_that_the_operator_did_it_and_moves_nothing(self):
        ok, sc = self._run()
        self.assertTrue(ok)
        args, kwargs = sc.move_balance.call_args
        self.assertEqual(args, ("tok", 0, ledger.RESOURCE_CHANGE))
        self.assertEqual(kwargs["detail"], {"actor": "operator", "action": "hibernate"})
        self.assertTrue(kwargs["write_zero"])

    def test_an_unknown_instance_writes_nothing(self):
        ok, sc = self._run(exists=False)
        self.assertFalse(ok)
        sc.move_balance.assert_not_called()

    def test_an_instance_that_is_already_hibernated_writes_nothing(self):
        ok, sc = self._run(hibernated=True)
        self.assertTrue(ok)
        sc.move_balance.assert_not_called()

    def test_without_privileges_nothing_is_written(self):
        ok, sc = self._run(admin=False)
        self.assertFalse(ok)
        sc.move_balance.assert_not_called()

    def test_a_refusal_writes_nothing(self):
        ok, sc = self._run(request=hibernation.HibernationRefused("no"))
        self.assertFalse(ok)
        sc.move_balance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
