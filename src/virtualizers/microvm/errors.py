class MicroVMError(RuntimeError):
    """A microVM could not be prepared, booted or torn down.

    One type for the whole family, including the backends. There used to be a
    ``CHExecuteError`` and a ``QEMUExecuteError``, but nothing outside a backend
    ever needed to tell the two apart -- their only effect was that a helper
    shared between the backends had to pick one of them to raise, which is how
    a QEMU launch came to fail with a Cloud Hypervisor error.
    """
