"""The booting state must already carry the shares a VM holds.

Shares are reserved before the VM is built, and the final runtime state is not
written until the guest answers on the network -- seconds later. A VM killed in
between is torn down from the state it has, so one that did not name its shares
would leave a virtiofsd daemon and a host directory behind with nobody left to
collect them.
"""
import unittest
from unittest.mock import patch

try:
    from src.virtualizers.microvm import runtime_state
    from src.virtualizers.microvm.members import CH
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    runtime_state = CH = None


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class BootingStateSharesTest(unittest.TestCase):
    def _booting_payload(self, **kwargs):
        captured = {}
        with patch.object(
            runtime_state, "save_runtime_state",
            side_effect=lambda vmachine_id, payload: captured.update(payload),
        ):
            runtime_state.save_booting_state(
                "vm-parent",
                hypervisor=CH,
                service_id="svc",
                pid=123,
                ip="10.0.0.2",
                mac="02:00:00:00:00:01",
                tap="tap0",
                bridge="br0",
                cleanup_rules=[],
                rule_comment_prefix="nodo-vm-parent",
                **kwargs,
            )
        return captured

    def test_held_shares_are_releasable_before_the_launch_finishes(self):
        payload = self._booting_payload(
            virtiofs=[{"share_id_hex": "a" * 64}], exported_shares=["a" * 64],
        )
        self.assertEqual(payload["virtiofs"], [{"share_id_hex": "a" * 64}])
        self.assertEqual(payload["exported_shares"], ["a" * 64])

    def test_a_vm_without_shares_carries_empty_lists(self):
        payload = self._booting_payload()
        self.assertEqual(payload["virtiofs"], [])
        self.assertEqual(payload["exported_shares"], [])


if __name__ == "__main__":
    unittest.main()
