"""What a Linux microVM booted on this host needs, whatever hypervisor boots it.

This is a *family* layer, not a base class and not "what every backend has".
Cloud Hypervisor and QEMU share the code in here because they are the same kind
of thing:

* a guest booted from one kernel + initramfs + ext4 rootfs bundle (``build``,
  ``bundle``, ``rootfs``),
* wired to a tap on one host bridge with a deterministic IP/MAC and DNAT for its
  published ports (``network``, ``firewall``),
* capped by a cgroup v2 on the hypervisor process (``cgroups``, ``limits``),
* tracked by a pid whose ``/proc`` name the node matches (``process``),
* recorded in one runtime-state store keyed by ``vmachine_id``
  (``runtime_state``), swept by one janitor (``maintain``),
* optionally handed shared directories over virtiofs (``virtiofs``).

A backend that has none of that -- a remote/cloud one with no pid and no local
tap, a containerised one that delegates isolation to its runtime -- does not
belong here and must not be made to fit. It registers itself in
``src.virtualizers.registry`` as its own family, brings its own store and its own
sweep, and never imports this package. See ``docs/BACKENDS.md``.

Deliberately re-exports nothing: pulling ``build`` or ``execute`` in here would
cost every importer the whole dependency tree (grpc, bee_rpc, the gateway),
including the ones that need none of it -- such as the guest floors in ``limits``.
Import the submodule you need.
"""
