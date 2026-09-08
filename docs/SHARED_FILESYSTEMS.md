# Shared filesystems — a parent → child capability

A shared directory is a **communication channel**. If an instance that is not a
child of the exporter could mount it, two instances the Networks model keeps in
separate communication domains would exchange data over disk: the isolation
`Service.Network` provides only means something if share gating is at least as
strict. Nothing cryptographic enforces this — only the node can — so it is a
node-conformance property, verifiable and punishable through reputation.

### The invariant (normative)

- **`shared` belongs only to the creator of the directory**: the instance whose
  own image contains it. There is no re-export and no transfer.
- **`guest` can only be exercised by that instance's direct children.** No one
  else, ever.
- **A service that is not a child of the exporter can never attach to the
  directory. A node that allows it is malicious.**

### Declaration

Reserved xattrs on directories in `Container.Filesystem.ItemBranch.xattrs`. There
is no `Service.Network` involvement.

| xattr | meaning |
| --- | --- |
| `shared=true` | this directory is **exported** to the children this instance launches |
| `guest=true` | this directory is **inherited** from the parent that launched this instance |
| `access=ro\|rw` | requested mount mode for a `guest` dir (default `rw`) |
| `share_tag=<tag>` | logical **name** of the share, used instead of the mount path |
| `share_env=<ENV_VAR>` | environment variable whose **value** picks the concrete share |

Everything decidable from a spec alone is rejected (`ValueError`) as soon as the
node reads it, which is what makes it validatable at pack time:

- `shared` and `guest` are mutually exclusive on one directory, and both are only
  valid on directories;
- `share_tag` / `share_env` are meaningless without one of them;
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
share_id      = H(parent_instance_id, name, share_env, discriminator)
```

The fields are **length-prefixed**, not separator-joined: two of them carry text
the spec chose, so with a separator a child could reach a share it was never
granted by pushing the separator into its own tag.

The **name of the variable** is part of the identity, not only its value. That is
what makes the match symmetric, the way `peer_env_matches` requires both sides to
agree: without it, a child declaring no `share_env` would derive the same id as a
parent that declares one but was launched with no value for it, skipping the
discriminator altogether. A variable that is declared on both sides but set on
neither is still a match — two instances missing the same variable are in the
same unnamed domain.

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

### Materialization (VirtioFS — implementation detail)

`src/virtualizers/microvm/shares.py` is where both hypervisors materialize a
guest's shares — one implementation, since the only thing that differs is how the
devices reach the guest. Authorization is asked again there, from the same
records: a backend that simply trusts what it is handed would be a single point of
failure for the one rule only the node can enforce.

1. One `virtiofsd` daemon per share on the host, exporting its directory over a
   Unix socket keyed by the share id (`--sandbox chroot`, deny-by-default). The
   exporting parent and every co-located child reuse it.
2. The first materialization of an export **seeds** its directory with the
   exporter's own packaged subtree at that path, read out of the offline image
   with `debugfs rdump` (rootless, like every other image access), so mounting
   the share does not hide what the exporter shipped. A child attaching later
   never reseeds; a handed-over rundev directory is never seeded at all.
3. One virtio-fs device per share on the guest's command line: cloud-hypervisor
   splices in the `--fs tag=…` arguments built for it, QEMU builds its own
   `vhost-user-fs` wiring from the same mount state.
4. A guest mount plan (`/.__nodo_virtiofs`, a JSON list of `{tag, path, ro}`)
   injected into the rootfs; guest init mounts each entry (`-o ro` for `ro`). The
   daemon is always read-write; `ro` is applied guest-side.
5. **Lifecycle by ownership.** A share's own state file records who is using it
   and which instance exports it, written *before* the VM that will use it is
   built. A share ends when its **exporter** leaves — it is that instance's
   storage, so nothing is left to hold it up or to be charged for it — and its
   guests then lose the directory, which is the whole of what being a guest
   means. If the exporter is already gone, the last user out ends what remains,
   so nothing outlives everyone. A guest leaving while the exporter runs changes
   nothing. A handed-over rundev directory is released but never deleted.

   A guest that loses its share mid-run keeps running with a mount point whose
   accesses fail. Nothing can be unmounted from outside the guest, so the choice
   is between that and killing an instance the node was not asked to kill; what
   makes it acceptable is that it cannot happen quietly at launch — a child whose
   exporter is already gone is refused before it starts, because authorization is
   asked again on the materialization path.

Write concurrency between participants is the application's problem, as it would
be over NFS; the node arbitrates nothing beyond the mount mode each guest asked
for.

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

There is no separate reservation per share, and no ceiling a guest could exceed
on its own: a guest cannot make the share grow beyond what its exporter is
willing to pay for, because it is the exporter that pays, and an exporter that
outgrows its balance is stopped by the mechanism that already stops one.

The service specification never mentions VirtioFS; swapping the backend requires
no protocol change.
