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

## Which architecture a service declares, and which ones a node announces

Two architectures match only when their formals are equal. So the compatibility is
in the announcement, not in the match:

- A service declares the most general architecture that its binaries need: for
  example `linux-arm64-any-page` for software that reads the page size when it runs,
  and `linux-arm64` (pages of 4096 bytes) for software that depends on 4096.
- A node announces each architecture that it can run in `Peer.resources`, also the
  more general ones: a node of AArch64 with pages of 4096 bytes announces
  `linux-arm64` and `linux-arm64-any-page`. The node reads the formals once, when it
  makes its announcement (REASON 2.5), and not at each match.
- A service with builds for some of the variants only (for example pages of 4096 and
  65536 bytes, but not 16384) is one service for each architecture (see
  `Container.architecture`). The services differ only in their architecture, so they
  share their filesystem.
- Do not write a formal for a combination of variants ("4096 or 65536"): the number
  of combinations grows with no limit.

The page size is a part of the architecture: the software sees it (`sysconf`, the
alignment of `mmap` and of the segments of a binary). The setting `guest-kernel`
cannot change it: a node refuses a kernel whose page size the architecture does not
allow.

## Notes

- The formal can say that the architecture boots a guest kernel, and which setting of
  `policies/container_init/` selects it. The kernel is not a part of the architecture.
- The benchmarks differ for each architecture, so the node announces them for each.
- Devices have their own folder: `devices/`.
