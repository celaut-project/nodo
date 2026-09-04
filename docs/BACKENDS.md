# Execution backends: what a backend is, and what a backend *family* shares

How a node's execution backends are layered, why the boundary sits where it does,
and what it takes to add a third one. The conceptual companion is
[`CONCEPTS.md`](CONCEPTS.md); the operator-facing view of the same machinery is
[`USAGE.md`](USAGE.md) and [`FIREWALL.md`](FIREWALL.md).

Two backends exist today. Both boot a Linux microVM on this host, so almost
everything they do is the same thing done twice — which is exactly the problem
this document settles: *which* of it is shared, at what layer, and on what
grounds.

## The layers

```
src/virtualizers/
  interface.py        the node's only door. Routes; implements nothing.
  registry.py         the backends this node has, and their families.
  selection.py        which backend runs a given service (by architecture).
  architecture.py     arch tags; backend-agnostic.
  firewall.py         transport vocabulary + the per-VM rule entry points.

  microvm/            ONE FAMILY: a Linux microVM booted on this host.
    members.py          who is in it (ch, qemu) and how each names itself.
    hypervisor.py       the whole membership contract: 4 facts per member.
    paths.py            CACHE/microvm/... — the family's disk layout.
    build.py            the one build cache: kernel + initramfs + ext4 rootfs.
    bundle.py           reading a built bundle back, and refusing a bad one.
    rootfs.py           offline guest injection via debugfs (no loop mounts).
    network.py          one bridge, one subnet, one IP/MAC allocator, taps, DNAT.
    firewall.py         the per-VM allow-list, written by comment prefix.
    cgroups.py limits.py  cgroup v2 enforcement and the guest floors.
    virtiofs.py         parent -> child shared filesystems.
    runtime_state.py    one enumerable store: shared index, private payloads.
    process.py          vm ids, visible process names, liveness.
    kill.py maintain.py  teardown, health check, janitor, prune scan.
    guest.py initramfs.py guest_panic.py serial.py host.py

  ch/                 Cloud Hypervisor: execute.py, hotplug.py. Nothing else.
  qemu/               QEMU/TCG: execute.py, hotplug.py, config.py, qmp.py.
```

Three layers, three different questions:

* **Neutral** (`interface`, `registry`, `selection`, `firewall`, `architecture`) —
  what the *node* needs from any backend. It names no hypervisor and imports no
  backend module.
* **Family** (`microvm/`) — what a group of backends shares *because they are the
  same kind of thing*. Not "what every backend has".
* **Backend** (`ch/`, `qemu/`) — only what genuinely differs.

## Question 1: where does the shared machinery live?

**Decision: a named family layer (`microvm/`), reached through a registry, with a
tiny explicit membership contract.**

What CH and QEMU share, they share because they are the same kind of thing: a
Linux microVM booted on *this host* from a kernel + initramfs + rootfs bundle,
wired to a tap on a bridge with DNAT, capped by cgroups v2, tracked by a pid whose
`/proc` name the node matches, with virtiofs for shared filesystems. That is a
real category with a name, and naming it is the whole point: it puts the shared
code somewhere that is *honestly* narrower than "every backend".

The alternative that was rejected is hoisting `runtime_state`, `cgroups`,
`virtiofs` and the tap/DNAT helpers straight up into `virtualizers/`. It breaks
the cycle too, but it promotes CH+QEMU-specific machinery into the position of
"what a backend is", which is false. A remote/cloud backend has no pid, no
`/proc`, no local tap, no cgroup — its runtime state is a handle to someone else's
API. A container backend has no kernel and no initramfs and delegates network and
CPU isolation to its runtime. Either would have to impersonate a shape that does
not fit, or bypass the layer entirely — which is today's problem with a different
module name.

### Why a family layer does not become the new `ch/`

That is the real risk in drawing a family boundary with only two members, so three
things hold it:

1. **The membership contract is four facts, and it is written down.**
   `microvm/hypervisor.py` is a frozen dataclass: a name, a log tag, the prefix of
   the launcher's visible `/proc` name, and the prefix of its control socket's
   filename. Nothing else. Given those, the family can boot-track, health-check,
   tear down and sweep a guest without knowing which hypervisor produced it.
2. **The family lists its own members, and nothing else can be in the list.**
   `microvm/members.py` is the only place `ch` and `qemu` are named together. A
   backend that is not a locally booted Linux microVM never appears in it.
3. **Launch and resize stayed in the backends.** `execute` and `hotplug` are where
   CH and QEMU genuinely differ (an API socket and KVM against a QMP socket and
   TCG; a cgroup move against a balloon resize). Neither is in the family layer,
   and there is no base class inviting them in.

### The registry, and why the neutral layer needs one

`interface.py` used to import both backends' `execute`, `kill`, `maintain` and
`hotplug` directly and branch on a name. That made the neutral layer depend on
every backend at import time, which in turn made a backend importing anything
neutral a cycle — the reason #295 had to break its own imports with function-local
`import` statements.

`registry.py` holds a module path and an attribute name per call, resolved when the
call is made. Two levels, because two different things are being routed:

| routed by | calls | why |
| --- | --- | --- |
| **instance** (`Backend`) | `execute`, `kill`, `maintain`, `hotplug` | what runs *one* guest |
| **family** (`Family`) | `build`, `is_built`, `remove_built`, `built_rootfs_size_bytes`, `billable_resources`, `sweep_orphans` | one build cache and one store per family, not per backend |

The lazy resolution is not laziness for its own sake: importing a launcher costs
its whole dependency tree (grpc, bee_rpc, the gateway), and the registry is
imported by everything that has to route anything — the firewall frontend, `nodo
instances`, the maintenance tick.

Family routing has one loose end, left loose on purpose. Three of those calls hold
a service *hash* and nothing else, and a hash says nothing about which backend
would run it — nor are their answers the kind that merge across families (a price
is one number, not a set). They ask the native backend's family. A second family
has to settle that, and settle a second question with it: `build` deliberately does
*not* route through `select_virtualizer`, because that raises for an architecture
this node cannot run, and building is not running — a node may hold and serve a
foreign-arch build with emulation switched off.

### The build cache is family-level, not node-level

The prior question the issue raised — *is the build cache CH's at all?* — has a
third answer, and it is neither.

It is not CH's: `interface.build()` already treated it as singular, and QEMU boots
the very bundles it writes. But it is not node-level either. What it produces is a
kernel, an initramfs and an ext4 rootfs image with the node's config injected
offline — the microVM boot contract, item for item. A container backend would build
an OCI layer instead; a remote backend would build nothing at all and ask whoever
runs the guest whether it is built.

So it sits on `Family`, not on `Backend` and not in the neutral layer, and
`CACHE/cloud_hypervisor/` became `CACHE/microvm/` (`microvm/paths.py`, which is now
the single statement of that layout — it used to be fifteen copies of
`Path(CACHE) / "cloud_hypervisor" / ...` across both backends' execute, kill, build
and state modules).

### What did *not* move: the config keys

Networking, sockets, kernel paths and floors are still configured under
`virtualizers.ch.*` even though the code reading them is now the family's. That is
deliberate. Runtime state is disposable (see **Constraints** below); a config key is
not — it is in every installed node's `config.yaml` and in the installer. Renaming
those keys is a user-visible change with its own migration story, and folding it
into a code move would have made a behaviour-preserving refactor into a breaking
one. It is a separate change.

## Question 2: the on-disk state

**Decision: one enumerable store per family — a shared index with private
payloads — swept per family, never per backend.**

The tension is real. The janitor's job is to find entries with *no database row*,
so it cannot ask the database who owns what: it must be able to enumerate orphans
generically. But the contents of an entry (pid, tap, mac, cleanup rules, which
socket) are meaningful only to whoever wrote them.

`microvm/runtime_state.py` splits the entry in two, and the split is load-bearing:

* **Index** — `vmachine_id`, `virtualizer`, `service_id`, `pid`, `process_name`,
  `created_at`, `booting`, `ip`. What any reader may interpret without knowing who
  wrote the entry. It is the minimum that makes an entry judgeable: whose it is,
  whether its process is still the one that was launched, and whether it is old
  enough to judge at all.
* **Payload** — `control_socket`, `cgroup_path`, `virtiofs`, `dnat_rules`,
  `boot_mem_bytes`, `guest_kernel_reserve_bytes`, … Owned by whoever wrote it, and
  opaque to everyone else.

`process_name` is the field that resolves #295. Liveness has to confirm the
recorded PID still belongs to *this* VM, which it does by matching the launcher's
visible `/proc` name — and those names differ per hypervisor. The janitor had only
a state file, so it *guessed* the name, and guessed CH: a healthy QEMU guest failed
CH's name test and was reaped as `stale_runtime_process_dead` seconds after boot.
Recording the name removes the guess, and with the guess goes the dispatch:
`ch.maintain._liveness_for`, `ch.maintain._kill_for` and the duplicate
`qemu/process.py` are all gone, and `kill` and `maintain` exist once each for the
whole family instead of twice over.

The same reasoning retires #295's other patch. The final state write did not carry
`virtualizer` at all — only the *booting* state did — so every reader of a
fully-booted guest had to fall back to CH. Both writes now carry it, and the
fallback is gone: an entry naming a hypervisor this family does not have is
reported and left alone, never guessed at (`microvm.members.member`).

### Why the store is shared, and why the sweep is per family

The store is shared because what it tracks is shared. There is one bridge, one
guest subnet, and one IP/MAC allocator that reads *every* entry to avoid handing
the same address out twice. Two backends allocating from stores they cannot see
each other's entries in would collide, and the collision would look like a guest
that boots and cannot be reached. `nodo prune`'s disk accounting has the same
problem, from the other side (#290).

So each backend owning its own directory is wrong for *this* family. But "the
janitor globs a directory" is wrong for backends in general — a remote backend has
no directory to glob. Both are satisfied by putting the sweep on `Family` rather
than on `Backend`:

* `interface.janitor_cleanup_orphans()` iterates the registered *families* and asks
  each to sweep its own orphans. One family's failure never stops another's.
* `microvm.maintain.sweep_orphans()` answers by enumerating the one store its
  members share, and dispatching each entry's teardown to the hypervisor that entry
  names.
* A family with no local store would answer the same question by querying whatever
  service holds its guests, and would read no directory at all.

There is exactly one definition of "orphan" in the codebase
(`microvm.maintain.orphan_reason`), used by both the janitor and `nodo prune`, so
the two can never disagree about a VM that `prune` lists and then refuses to
remove.

## Adding a third backend

### Another Linux microVM (Firecracker)

A new member of the existing family. In order:

1. Five lines in `microvm/members.py`: its name, log tag, process-name prefix and
   control-socket prefix.
2. `firecracker/execute.py` — its own launch. It calls the family for the bundle,
   the tap, the rootfs injections, the cgroup and the state writes, and contributes
   only its own binary invocation and its own control socket.
3. `firecracker/hotplug.py` only if its resize knob differs from a cgroup move; if
   it does not, point the registry at CH's.
4. One `Backend` entry in `registry.py` and one arm in `selection.py`.

`kill`, `maintain`, the janitor, `prune`, the firewall and the build cache need no
change at all — which is the test of whether the boundary was drawn in the right
place.

### Something structurally different (remote, or containerised)

A new *family*. It writes a `Family` and one or more `Backend` entries in
`registry.py`, and it never imports `microvm/`.

A **remote** backend: `execute` calls somebody else's API and gets back a handle;
its "runtime state" is that handle, so its store is whatever it needs to map
`vmachine_id` to it, and its `sweep_orphans` lists remote instances and reconciles
them against the local rows. It has no pid, so `process.py` is meaningless to it;
no tap, so the per-VM firewall entry points have nothing to write and the node has
to decide whether it even asks for a rule (see the note at the top of
`virtualizers/firewall.py`); no local build, so `build`/`is_built` become questions
for the remote side.

A **containerised** backend: it builds an image in its runtime's format rather than
an ext4 rootfs, and delegates network and CPU isolation to that runtime — so
`network.py` and `cgroups.py` do not apply, and its `Family` answers `build` and
`sweep_orphans` in its runtime's terms.

Neither has to impersonate a microVM, and neither can be forced to. That is what
the layering buys, and it is why the shared machinery was not hoisted into the
neutral layer.

## Constraints this refactor was held to

* **No migration.** Existing runtime state and instances are disposable, so
  `CACHE/cloud_hypervisor/` → `CACHE/microvm/` and `api_socket`/`qmp_socket` →
  `control_socket` are plain renames with no backfill and no compatibility shim.
* **CH must not regress.** Native-arch launches are the default path and the
  performance baseline. Nothing in CH's launch sequence changed; the helpers it
  calls moved module, and the calls are the same calls in the same order.
* **Behaviour-preserving**, with one deliberate exception: a QEMU guest's control
  socket is now health-checked, because only CH's copy of `maintain` used to look.
  An emulator that has dropped its QMP socket is as gone as a cloud-hypervisor that
  dropped its API socket, and used to be reported healthy.

## Related

* #299 — this decision.
* #295 — the tactical patch that made the entanglement visible.
* #290 — `nodo prune`, which reasons about the same shared store.
* #98 — firewall logic refactor.
