"""Cloud Hypervisor: boot a native-arch service as a microVM under KVM.

Only what is CH's lives here -- its launch (``execute``) and its resize
(``hotplug``). What it shares with QEMU because they are the same kind of thing
-- the build cache, the bundle, the rootfs injections, host networking, cgroups,
the runtime-state store, liveness, teardown and the janitor -- lives in
``src.virtualizers.microvm``.

Deliberately re-exports nothing: re-exporting ``execute`` here would make every
module in the package cost its whole dependency tree -- grpc, bee_rpc, the
gateway -- including the ones that need none of it. Import the submodule you
need, and reach a backend through ``src.virtualizers.interface`` or
``src.virtualizers.registry`` rather than by importing it.
"""
