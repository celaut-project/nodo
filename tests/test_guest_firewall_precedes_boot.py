"""The guest's egress policy must be committed before the guest can run at all.

`configure_guest_firewall_policy` (block_all + the allow-list) used to run after
the guest-network readiness wait -- seconds after `subprocess.Popen`, and well
after `create_tap` enslaved the guest's tap to the bridge and made it
forwarding-capable. Nothing that call needs (vm_ip, network_resolution, the
gateway address/port) comes from the guest itself; all of it is resolved earlier
in `execute()` from the manifest and config. So the gap between "guest can forward
packets" and "nodo's own allow-list exists" was pure ordering, not a data
dependency -- and during it, a booting guest's traffic answered only to the host's
own default FORWARD policy (unrestricted on a plain install), not to nodo's.

`execute()` runs real subprocesses, taps and cgroups, which makes mocking its full
body for a call-order assertion disproportionate to what is being protected here.
Instead this reads the function's own source and checks the one property that
matters: the policy is committed after the tap exists and before the hypervisor
process starts, so a future edit that moves it back after `Popen` fails a fast,
dependency-free test instead of only showing up as a live firewall gap.

Both launchers are checked. The policy itself is one implementation now, but the
*ordering* is each launcher's own -- it is a property of the sequence in
`execute`, and nothing in the family layer can enforce it.
"""
import inspect
import unittest

IMPORT_ERROR = None
try:
    from src.virtualizers.ch import execute as ch_execute
    from src.virtualizers.qemu import execute as qemu_execute
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_execute = None  # type: ignore[assignment]
    qemu_execute = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FirewallBeforeBootOrderingTests(unittest.TestCase):
    def _assert_firewall_precedes_boot(self, module, firewall_call, popen_needle="subprocess.Popen("):
        source = inspect.getsource(module.execute)

        tap_at = source.index("network.create_tap(")
        firewall_at = source.index(firewall_call)
        popen_at = source.index(popen_needle)

        self.assertLess(
            tap_at, firewall_at,
            "the firewall policy must be configured after the tap is enslaved to "
            "the bridge (there is nothing to attach a FORWARD rule's effect to "
            "before that), so it is verified rather than assumed",
        )
        self.assertLess(
            firewall_at, popen_at,
            "the guest firewall policy must be committed before the hypervisor "
            "process starts -- moving it after Popen (or after the readiness "
            "wait) reopens the window in which a booting "
            "guest's traffic is governed by the host's default FORWARD policy "
            "instead of nodo's allow-list",
        )
        # And not merely present twice with one copy left in the old spot: it must
        # be a single call, in the new spot only.
        self.assertEqual(source.count(firewall_call), 1)

    def test_ch_backend_commits_the_policy_before_the_vm_boots(self):
        self._assert_firewall_precedes_boot(
            ch_execute, "network.configure_guest_firewall_policy(\n"
        )

    def test_qemu_backend_commits_the_policy_before_the_vm_boots(self):
        self._assert_firewall_precedes_boot(
            qemu_execute, "network.configure_guest_firewall_policy(\n"
        )

    def test_the_policy_call_still_precedes_network_readiness_too(self):
        # A weaker but very concrete regression: whatever else changes, the policy
        # must not be waiting on the guest to answer a ping.
        for module in (ch_execute, qemu_execute):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module.execute)
                firewall_at = source.index("network.configure_guest_firewall_policy(")
                ready_at = source.index("network.wait_guest_network_ready(")
                self.assertLess(firewall_at, ready_at)


if __name__ == "__main__":
    unittest.main()
