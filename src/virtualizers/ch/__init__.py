"""Cloud Hypervisor virtualizer.

Deliberately re-exports nothing: re-exporting ``build`` or ``execute`` here would make
every module in the package cost their whole dependency tree -- grpc, bee_rpc, the
gateway -- including the ones that need none of it, such as the guest floors in
``limits``. Import the submodule you need:
``from src.virtualizers.ch.limits import billable_resources``.

The virtualizer's public surface is ``src.virtualizers.interface``, not this package.
"""
