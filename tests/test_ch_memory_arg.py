"""cloud-hypervisor's ``--memory`` for a guest that has shared filesystems.

A virtio-fs device talks to its virtiofsd over vhost-user, which shares the guest's
memory with that process. cloud-hypervisor refuses to start such a guest without
``shared=on`` ("Using vhost-user requires using shared memory or huge pages"), and
only guests that have shares should pay for shared memory.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.virtualizers.ch import execute as ch
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChMemoryArgTests(unittest.TestCase):
    def test_a_guest_with_shares_gets_shared_memory(self):
        self.assertEqual(ch._ch_memory_arg(1907, shared=True), "size=1907M,shared=on")

    def test_a_guest_without_shares_keeps_private_memory(self):
        # Unchanged for every ordinary service.
        self.assertEqual(ch._ch_memory_arg(256, shared=False), "size=256M")


if __name__ == "__main__":
    unittest.main()
