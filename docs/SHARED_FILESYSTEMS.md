# Shared filesystems — a parent → child capability, or a network one

A shared directory is a **communication channel**. If an instance that is not a
child of the exporter could mount it, two instances the Networks model keeps in
separate communication domains would exchange data over disk: the isolation
`Service.Network` provides only means something if share gating is at least as
strict. Nothing cryptographic enforces this — only the node can — so it is a
node-conformance property, verifiable and punishable through reputation.

There are two kinds of share. A **parent share** (`shared` / `guest`) goes from
an instance to the children it launches; the instance tree is its scope. A
**network share** (`share_network`, #268) is for instances with no common parent;
a `Service.Network` is its scope. Network shares are described in
[their own section](#network-shares--no-common-parent); the other sections are
about parent shares, unless they say otherwise.

### The invariant (normative)

- **`shared` belongs only to the creator of the directory**: the instance whose
  own image contains it. There is no re-export and no transfer.
- **`guest` can only be exercised by that instance's direct children.** No one
  else, ever.
- **A service that is not a child of the exporter can never attach to the
  directory. A node that allows it is malicious.**

### Declaration

Reserved xattrs on directories in `Container.Filesystem.ItemBranch.xattrs`. A
parent share does not involve `Service.Network`; a network share does.

| xattr | meaning |
| --- | --- |
| `shared=true` | this directory is **exported** to the children this instance launches |
| `guest=true` | this directory is **inherited** from the parent that launched this instance |
| `access=ro\|rw` | requested mount mode for a `guest` dir (default `rw`) |
| `share_tag=<tag>` | logical **name** of the share, used instead of the mount path |
| `share_env=<ENV_VAR>` | environment variable whose **value** picks the concrete share |
| `share_network=<tag>` | shared with the members of the network with that tag (a [network share](#network-shares--no-common-parent)) |

A packed service gets these xattrs from the `shared_filesystems` field of
`service.json`; see [PACKING.md](PACKING.md#shared_filesystems).

Everything decidable from a spec alone is rejected (`ValueError`) as soon as the
node reads it, which is what makes it validatable at pack time:

- `shared` and `guest` are mutually exclusive on one directory, and both are only
  valid on directories;
- `share_network` excludes both of them;
- `share_tag` / `share_env` are meaningless without one of the three;
- **a share declaration cannot be nested inside another.** A `shared` inside a
  `guest` subtree is the re-export the invariant forbids, and two overlapping
  declarations are two mounts covering one path with the winner decided by the
  order of the mount plan — undefined, not a policy;
- **two directories on the same side cannot carry the same name.** A path cannot
  repeat in a tree, so path-named shares never collide; a repeated `share_tag`
  would resolve two directories to one share, hence one virtio-fs tag emitted as
  two devices;
- `share_tag` is matched **literally and case-sensitively**, after stripping
  surrounding whitespace, and is restricted to `[A-Za-z0-9][A-Za-z0-9._@:+-]*`, so
  a stray `\n` cannot silently break matching. `share_env` must be a POSIX
  variable name.

### Identity

```
name          = share_tag        if declared, else the exported path
discriminator = env[share_env]   if share_env is declared, else ""
share_id      = H("parent", parent_instance_id, name, share_env, discriminator)
```

The first field keeps parent shares and network shares in separate id spaces.

The fields are **length-prefixed**, not separator-joined: two of them carry text
the spec chose, so with a separator a child could reach a share it was never
granted by pushing the separator into its own tag.

The **name of the variable** is part of the identity, not only its value. That is
what makes the match symmetric -- both sides must agree on which variable
discriminates, not just on its value: without it, a child declaring no `share_env`
would derive the same id as a parent that declares one but was launched with no
value for it, skipping the discriminator altogether. A variable that is declared
on both sides but set on neither is still a match — two instances missing the
same variable are in the same unnamed domain.

| declared | `share_id` | effect |
| --- | --- | --- |
| neither | `H(father, path, "", "")` | the share is the path |
| only `share_tag` | `H(father, tag, "", "")` | independent of where either side mounts it |
| only `share_env` | `H(father, path, env, value)` | tied to the path, one share per value |
| both | `H(father, tag, env, value)` | the full case |

A share is **not durable, and the tag does not make it so**: `parent_instance_id`
stays in the formula, and the directory is storage of the instance that created
it, so a share lives exactly as long as *that incarnation of the parent*. An
exporter that restarts gets a new instance id, hence a new and empty share, with
no path back to the old one; an exporter that stops takes the share with it, and
its guests lose the directory. The Hadoop example below invites the opposite
assumption, so it is worth stating plainly: this is not a persistence mechanism.

### Authorization is a precondition, checked before anything is spent

`guest=true` is an **execution precondition, not an option**. There is no
`required` xattr because it is always required: a service that declares an
inherited directory cannot run unless an instance exists that created that
directory and exported it to *this* one. If it does not, the launch **fails**.
Starting anyway would be the worst of both options — from inside a guest, a
datanode writing to its local disk and a job returning an empty result are
indistinguishable from "the share is empty".

So the check cannot live in the backend, at the end of the chain, after MU has
been spent and a rootfs built. `authorize_shares` (`src/manager/shares.py`) runs
as a **preflight in `launch_service`, before the balancer loop** — and
deliberately not inside `_detect_local_preflight_failure`, which reports a local
failure worth trying another candidate for. There is no other candidate: if the
share does not attach, the service is runnable on **no** peer.

It reads the parent's exports from the node's own records — its service spec from
the registry, and the environment values **from the parent's launch**, not any
current ones, since those are what the exporter resolved its share ids from at
boot. A spec that cannot be read is a refusal, not an empty grant.

Granting is **set membership against the direct parent, always**. The ancestor
tree is never walked — unlike `filter_networks_with_ancestors`, whose AND over
the chain is correct because a Network *is* delegated. A share is not: the parent
exports a directory out of its own image and needs no ancestor's permission, so
there is no induction to apply.

A refusal says which of two opposite causes it is, because a hash on its own can
only report non-membership:

- the parent does not export that name at all → **composition error** (the
  service was launched by a parent that does not provide the share);
- it exports the name under a different variable or a different value →
  **configuration error** (a typo, or a variable left undefined).

That is why `ShareRef` carries the name, the variable and its value in the clear
alongside the id.

### Cases

| case | outcome |
| --- | --- |
| **Top-level** (`nodo execute` of a service with `guest`) | **Not runnable.** A client has no filesystem to export. This is also what closes the recycled-dev-client-id leak: those ids come from a small reusable pool, so two unrelated `execute` calls could otherwise reach one another's directory. |
| **Parent on another node** | Not runnable there: the export is materialized from the parent's own rootfs, on the parent's own node. |
| **Sibling ↔ sibling** | Not runnable. Two children of one parent declaring the same guest path derive the same id, but neither is granted it unless the parent actually exports it. |
| **`rundev`** | The one legitimate exception, and explicit rather than accidental: a `nodo ggconf` sandbox declares what it exports in a `__shares__` JSON file beside its `__config__`, each entry naming a host directory to hand over (`{tag, dir, env, value, path, access}`). That directory is the developer's own — never seeded, never deleted by the node. |
| **Seeding** | Unambiguous: since the parent must exist and must have exported, **the first materialization is always the parent's**. No child can touch the directory first. |
| **The exporter stops while a guest runs** | The share goes with it and the guest loses the directory — it is the exporter's storage, and the guest never declared nor paid for it. A guest launched *after* the exporter is gone is refused outright, so this can only ever be a running instance losing a mount, never a new one starting without it. |

### Node placement

A service that declares any `guest` directory **must** run on the same node as
its parent. `launch_service` detects it via `service_requires_parent_colocation()`
and pins execution locally, skipping delegation. With the preflight ahead of it,
that predicate is never exercised on a share that does not exist.

Worth keeping in mind: because shares force colocation, they are a **scheduling**
capability as well as a data one — a chain of nested services can pin an
arbitrarily deep subtree onto one node. That is a resource-exhaustion
consideration, not an authorization one.

### What a spec does *not* tell you

Seeing that a service declares no shared directory **does not** guarantee that
none of its descendants have shares:

- `shared` is not a delegated right, so there is no induction and therefore **no
  confinement** — unlike Networks, where the AND over the chain does confine every
  descendant.
- A grandchild's spec is not inspectable: a content-addressed service does not
  declare which dependencies it will launch, it requests them at runtime by hash.
- All a child's spec tells you is that it will receive nothing of yours, expose
  its image to no child of its own, and not pin to your node by colocation.
  Nothing about the subtree.
- Where the fan-out lands is decided **by the balancer at the first hop**, not by
  whoever launches: if the child was delegated to a peer, the subtree pins there.
  It cannot be vetoed or foreseen.
- `share_tag` makes inspectability worse, not better: the spec no longer says
  which host directory is touched, because the discriminator is resolved at
  runtime from an environment value.

### The Hadoop case

```
# coordinator (the parent)
/data       shared=true, share_tag=hdfs-data, share_env=HDFS_CLUSTER

# namenode + N datanodes (its children)
/mnt/hdfs   guest=true,  share_tag=hdfs-data, share_env=HDFS_CLUSTER, access=rw
```

Launched with `HDFS_CLUSTER=prod`, the N+1 instances derive one `share_id` and
mount one host directory — each child wherever suits it, not wherever the parent
put it — all pinned to the coordinator's node. The same datanode image serves
another cluster by being launched with another `HDFS_CLUSTER`: services are
content-addressed, so keeping the dataset out of the specification is what keeps
it one service id, one cached image and one accruing reputation instead of a
repack per dataset.

### Network shares — no common parent

A parent share needs an instance that is the parent of every participant. Two
instances that are launched independently — for example, six services that a
script launches one by one — have no such instance. For them, the scope is a
**network**: two instances share a directory only when both declare the same
`Service.Network` and the same share.

The network is the correct scope because a share is a communication channel.
Instances that declare the same network, with the same `formal` values, can
already reach each other over TCP (`local_network_instances` gives them as peers).
A network share gives them no channel that the network does not give them. An
instance that does not declare the network cannot join the share.

#### Declaring a network share

```
/data   share_network=hdfs, share_tag=hdfs-data, share_env=HDFS_CLUSTER
```

- `share_network=<tag>` names the entry of `Service.network` that carries that
  tag. The service must declare that network; a spec that does not is refused
  when the node reads it, and at pack time.
- `share_tag`, `share_env` and `access` work as for a parent share.
- A network share is **symmetric**: there is no `shared` or `guest` side, and it
  cannot be combined with either.
- In `service.json`: `{"path": "/data", "role": "network", "network": "hdfs",
  "tag": "hdfs-data"}`. See [PACKING.md](PACKING.md#shared_filesystems).

#### Identity of a network share

```
formal    = the network's formal, with every ${VAR} substituted ("" if none)
share_id  = H("network", tag, formal, name, share_env, discriminator)
```

- The network takes the place of the parent instance id. Every member that
  declares the same tag, `formal`, name, variable and value derives the same id,
  whichever node and whichever parent it runs under.
- A tag is enough. When the network has a `formal`, it is part of the id after
  substitution, so `cluster=${HDFS_CLUSTER}` gives one share per cluster.
- An unanswered `${VAR}` refuses the launch. The id is not complete without it,
  and a guess would put the instance in the share of another domain.
- The id does **not** use `match_networks`. That relation is not transitive (one
  common tag is enough), so it cannot give one id. A member whose network has a
  `formal` and a member whose network has none are in **two different shares**,
  also when `match_networks` says that the networks are the same.

#### Authorizing a network share

There is no exporter to ask. A network share is granted when the instance is in
the network:

- the operator's `service_networks` policy allows the network. `launch_service`
  enforces it on every declared network before the shares are checked;
- for an instance that a local instance launches, every ancestor declares the
  network (`filter_networks_with_ancestors`). An instance whose parent cannot
  reach a domain cannot open a channel into it over disk.

The check is `authorize_network_shares` (`src/manager/shares.py`). It runs in
`launch_service` before the balancer, and again on the materialization path.

#### Placement: `Gateway.LocateShare`

A network share is a directory on one host. A member that runs on another node
would derive the same id there and get a new, empty directory. So before a member
is placed, the node finds which node holds the share
(`src/manager/share_location.py`):

1. It looks at its own share state.
2. It asks each peer with `Gateway.LocateShare(share_id)`. A peer answers
   `held=true` when it has the directory and at least one instance uses it. A
   parent share is never reported.
3. If one node holds the share, the balancer runs the member **only on that
   node**. If no node holds it, placement is free, and the node that runs the
   member creates the share.

With `network.DELEGATE_EXECUTION` off, no peer is asked. A peer that cannot answer
(unreachable, timed out, or older than `LocateShare`) is taken as not holding the
share.

`LocateShare` tells a caller only that a share id is in use. To calculate the id,
the caller must know the tag, the `formal` and the share's name and value — which
is what joining the share takes anyway.

#### Known limit: two nodes can create one share

The lookup is not consensus. If two members of a new share are placed at the
same time on two nodes, both nodes can create the share: one name, two
directories. Data cannot be merged after that without moving it, so the node
does not hide it. A later launch that finds the share on more than one node is
not refused: those nodes are the only candidates, the balancer chooses the
cheapest of them, and the log warns with every location. The copies stay
separate. A member whose shares are held on different nodes, with no node that
holds them all, is refused, because one instance cannot run on two nodes.

#### Lifecycle of a network share

- **No owner.** The first member that materializes the share seeds it with its
  own packaged subtree at its path. The check, the seed and the reservation run
  under the share's lock, so two members that start together cannot both seed it.
  If the seed fails, the directory is removed, and the next member seeds it again.
- **It ends with its last member.** A member that stops removes only itself. When
  the last member stops, the directory is removed. A member that reserves the
  share while the last one stops keeps it: the directory is removed only if
  nobody reserved it again.
- It is **not durable**: a share with no members has no data.
- A read-only image (`read_mode=ro`) can be a member, and can export or inherit a
  share. It mounts every share read-only, whatever `access` says, so it sees what
  the others write and only instances that are not read-only write. When it is
  the first member, it seeds the share from its squashfs or erofs image.

#### Accounting of a network share

A network share has no owner, so no single member's storage is the whole of it.
Each member is counted an **equal part** of the share's bytes, from the number of
members it has now. The parts of the current members add up to the directory, so
the host's disk ceiling counts it one time. The part changes when members join or
leave, and the maintenance tick calculates it again.

#### Example: six services, no common parent

```
# coordinator, namenode and four datanodes, each launched by a script
network:  tags=[hdfs], formal="cluster=${HDFS_CLUSTER}"
/data     share_network=hdfs, share_tag=hdfs-data
```

Launched with `HDFS_CLUSTER=prod`, the first instance creates the share on the
node that runs it. Each next instance finds the share there with `LocateShare`
and runs on that node. The same images with `HDFS_CLUSTER=test` make a second,
separate share.

### Materialization (VirtioFS — implementation detail)

`src/virtualizers/microvm/shares.py` is where both hypervisors materialize a
guest's shares — one implementation, since the only thing that differs is how the
devices reach the guest. Authorization is asked again there, from the same
records: a backend that simply trusts what it is handed would be a single point of
failure for the one rule only the node can enforce.

1. One `virtiofsd` daemon **per VM and share**, exporting the share's directory
   over a Unix socket keyed by both (`--sandbox chroot`, deny-by-default). A
   `virtiofsd` serves a single vhost-user client: a second VM connected to the
   same socket is queued by the kernel and never answered, so its hypervisor waits
   in the handshake and the guest never boots, and the daemon exits when its one
   client disconnects. So the exporting parent and each co-located child get a
   daemon of their own, all exporting the one host directory (#480).
2. The first materialization of an export **seeds** its directory with the
   exporter's own packaged subtree at that path, read out of the offline image
   with `debugfs rdump` (`unsquashfs` or `fsck.erofs` for a read-only image;
   rootless, like every other image access), so mounting
   the share does not hide what the exporter shipped. A child attaching later
   never reseeds; a handed-over rundev directory is never seeded at all.
3. One virtio-fs device per share on the guest's command line: cloud-hypervisor
   splices in the `--fs tag=…` arguments built for it, QEMU builds its own
   `vhost-user-fs` wiring from the same mount state.
4. A guest mount plan (`/.__nodo_virtiofs`, a JSON list of `{tag, path, ro}`)
   injected into the rootfs; guest init mounts each entry (`-o ro` for `ro`). The
   daemon of a guest that asked for `ro` also runs `--readonly`, so the host
   refuses its writes: the guest's `-o ro` alone is a choice the guest could undo,
   since it is root in its own VM and can remount the device read-write. The
   exporter's daemon is always read-write.
5. **Lifecycle by ownership.** A share's own state file records who is using it
   and which instance exports it, written *before* the VM that will use it is
   built. A share ends when its **exporter** leaves — it is that instance's
   storage, so nothing is left to hold it up or to be charged for it — and its
   guests then lose the directory, which is the whole of what being a guest
   means. If the exporter is already gone, the last user out ends what remains,
   so nothing outlives everyone. A guest leaving while the exporter runs stops its
   own daemon and changes nothing else. Ending a share stops every daemon still
   serving it. A handed-over rundev directory is released but never deleted.

   A guest that loses its share mid-run keeps running with a mount point whose
   accesses fail. Nothing can be unmounted from outside the guest, so the choice
   is between that and killing an instance the node was not asked to kill; what
   makes it acceptable is that it cannot happen quietly at launch — a child whose
   exporter is already gone is refused before it starts, because authorization is
   asked again on the materialization path.

A member of a network share is materialized the same way, with these
differences: its daemon follows the `access` it declared, the first member seeds
the directory, and the share's state records no owner (see
[Lifecycle of a network share](#lifecycle-of-a-network-share)).

Write concurrency between participants is the application's problem, as it would
be over NFS; the node arbitrates nothing beyond the mount mode each guest asked
for.

### Host requirement: the Rust virtiofsd

The node starts the daemon with the flags of the Rust
[virtiofsd](https://gitlab.com/virtio-fs/virtiofsd) (`--socket-path`,
`--shared-dir`, `--sandbox`, `--cache`). `virtualizers.ch.VIRTIOFSD_BINARY` names
it. Two things do not work:

- The old C daemon from QEMU, `/usr/lib/qemu/virtiofsd` (from
  `qemu-system-common` up to 7.x). It does not accept these flags.
- A binary that only your user can find. The node runs as root, so
  `~/.cargo/bin` is not on its `PATH`. Use an absolute path.

There is no distro package for the Rust daemon on Ubuntu 22.04.

**The installer** puts virtiofsd 1.14.0 at `<MAIN_DIR>/bin/virtiofsd` and writes
that absolute path to `VIRTIOFSD_BINARY` (`bash/lib_virtiofsd.sh`):

- x86_64: the static binary that upstream attaches to the release. The zip and
  the binary in it are pinned by SHA-256.
- arm64 (upstream publishes no binary), or when the download fails:
  `cargo install virtiofsd --version =1.14.0 --locked`, in the node's own Rust
  toolchain (`dependencies.rust.RUNTIME_ROOT`), after it installs
  `libcap-ng-dev`, `libseccomp-dev` and `pkg-config` (`libcap-ng-devel`,
  `libseccomp-devel` and `pkgconf-pkg-config` on Fedora).

If `VIRTIOFSD_BINARY` is already a path of your own, the installer does not
change it and installs nothing. If the installer cannot get the daemon, the
install still completes, and only services with shared directories cannot run.

**By hand:**

```bash
sudo apt-get install -y libcap-ng-dev libseccomp-dev pkg-config
cargo install virtiofsd --locked
sudo install -m 0755 ~/.cargo/bin/virtiofsd /nodo/bin/virtiofsd
sudo nodo config set virtualizers.ch.VIRTIOFSD_BINARY=/nodo/bin/virtiofsd
```

**Check it** with `sudo nodo doctor`. The `virtiofsd (shared filesystems)` part
gives the resolved path and says if it is the Rust daemon, the old QEMU daemon, or
missing. A missing daemon is a `[WARN]`, not a failure, because only services
with shared directories need it.

**At launch**, the node probes the daemon (`--version`) for a service that
declares shared or guest directories, before it charges the client or builds a
rootfs (`src/virtualizers/microvm/virtiofsd.py`). If the daemon is missing or is
the old one:

- a service with a `guest` directory is refused. It can only run on this node.
- a service that only exports (`shared`) is not run on this node. A peer can
  still take it. If no peer does, the launch error gives the reason.

### Accounting

A share lives outside every rootfs, so its bytes are in no instance's recorded
disk unless they are put there. They belong, whole and undivided, to the
**instance that exports it**: the directory is seeded from that instance's own
image, at the path it declared, and only it is there for as long as the share is.
Its guests declare no ceiling covering it and are charged nothing for it.

That makes the exporter's `at_init.disk_space` and `at_most.disk_space` cover its
shares as well as its image — a service that exports a directory has to say so in
the disk it asks for, and if a share needs to grow further, the exporter raises
its own disk the same way it raises anything else, because the share is part of
it.

The figure lands on the exporter's row in `local_instances`, which is the one
place everything else reads it from:

- the maintenance tick charges it,
- `host_limits.committed` adds it into the host's disk ceiling,
- and the next launch is admitted against what is left.

An image is a fixed-size file and cannot grow; a share directory can. So an
exporter's disk is the one figure that is **re-derived** on each tick — the size
of the image plus the size of the directories — and written back to its row when
it moved. An instance that exports nothing is never measured and its row is never
touched.

A network share is counted differently: each member pays an equal part (see
[Accounting of a network share](#accounting-of-a-network-share)).

There is no separate reservation per share, and no ceiling a guest could exceed
on its own: a guest cannot make the share grow beyond what its exporter is
willing to pay for, because it is the exporter that pays, and an exporter that
outgrows its balance is stopped by the mechanism that already stops one.

The service specification never mentions VirtioFS; swapping the backend requires
no protocol change.
