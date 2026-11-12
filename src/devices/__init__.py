"""Devices with a direct connection (#536): a device that the node attaches to the
microVM of one instance, for example a GPU through VFIO.

* :mod:`spec` reads the ``Device`` messages of ``celaut.proto``: the tag format, the
  validation of a service, matching and the exclusive assignment.
* :mod:`host` reads the operator's devices from sysfs and checks them (doctor).
* :mod:`inventory` turns them into ``Peer.devices`` and answers availability.
* :mod:`assignments` keeps which instance holds which device, and the quarantine.
* :mod:`attach` gives the Cloud Hypervisor arguments, and resets a device.

Version 1 is experimental and off by default (``devices.ENABLED: false``).
"""
