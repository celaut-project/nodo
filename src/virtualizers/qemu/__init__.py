"""QEMU/TCG: boot a foreign-arch service under software emulation.

Only what is QEMU's lives here: its launch (``execute``), its balloon-based
resize (``hotplug``), its QMP client (``qmp``) and its per-arch emulator lookup
(``config``). Everything a locally booted Linux microVM needs regardless of
hypervisor comes from ``src.virtualizers.microvm``, which is also where the
teardown and health check of a QEMU guest live.

Re-exports nothing, for the same reason as the CH package: importing this must
not cost the launcher's dependency tree.
"""
