# Architectures

What the software of a service programs against: the instruction set, the ABI, and
what the node gives to the process (for example a virtual machine with a guest kernel,
or a container that shares the kernel of the host). The formal fixes the granularity.

Class: `architecture`.

## Places

- `Service.Container.architecture`: a requirement for the node that runs the service.
  It is never empty. A node that does not run the architecture refuses the service.
- `ArchitectureResources.architecture` in `Peer.resources`: an announcement of the
  node, with the most resources and the measured benchmarks of that architecture.

## Value

None: an architecture is a `Descriptor`. Two architectures are one only if their
formals are equal. A family (for example x86_64 at two levels of instruction set) is
one descriptor for each member (rule 5.3 of `../REASON.md`).

## Notes

- The formal can say that the architecture boots a guest kernel, and which setting of
  `policies/container_init/` selects it. The kernel is not a part of the architecture.
- The benchmarks differ for each architecture, so the node announces them for each.
- Devices have their own folder: `devices/`.
