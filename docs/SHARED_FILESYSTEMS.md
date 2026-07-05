# Networks & Shared Filesystems

This node separates two concerns that used to be tangled together in
`Service.Network`:

| Concern | Where it lives now |
| --- | --- |
| Peer discovery & communication | `Service.Network` |
| Execution environment (env vars, filesystem) | `Service.Container` |
| Filesystem sharing between instances | `Container.Filesystem` xattrs (parent → child) |
| VirtioFS | runtime implementation detail only |

## 1. `Service.Network` — a logical communication domain

A `Network` describes **which peers a service wants to talk to** and **which
protocols those peers must expose**. Nothing about disks.

```proto
message Network {
    repeated string tags = 1;
    string prose = 2;
    bytes formal = 3;

    // Protocols expected from the peers in this communication domain.
    repeated Api.Protocol protocol_stack = 4;

    // Name of an environment variable used to filter compatible peers during
    // ConfigurationFile.NetworkResolution. Only peers whose value for this
    // variable matches the requester's value are returned. Empty => no filter.
    string environment_variable = 5;
}
```

**Env-based peer filtering.** Many instances of the same service may exist on a
network, but a service often only wants the subset that shares some property.
Example: many PostgreSQL instances exist, but a client only wants those in its
own `PG_CLUSTER`. Set `environment_variable = "PG_CLUSTER"`; resolution then
returns only peers whose `PG_CLUSTER` equals the requester's. Implemented in
`src/manager/network_env.py`, consumed by `resolve_network()`.

## 2. `Service.Container` — the execution environment

Environment variables moved here from `Service.Api` (they describe the process
that runs, not the communication interface):

```proto
message Container {
    // ...
    map<string, DataFormat> environment_variables = 7;
}
```

## 3. Shared filesystems — parent → child inheritance

Filesystem sharing is an execution-environment concern, expressed through
reserved xattrs on directories in `Container.Filesystem.ItemBranch.xattrs`.
There is **no** `Service.Network` involvement and **no** public mechanism for
attaching to some other instance's filesystem: **the parent launching the child
is the authorization.**

| xattr | meaning |
| --- | --- |
| `shared=true` | this directory is **exported** to the children this instance launches |
| `guest=true` | this directory is **inherited** from the parent that launched this instance |
| `access=ro\|rw` | requested mount mode for a `guest` dir (default `rw`) |

`shared` and `guest` are mutually exclusive on a single directory, and both are
only valid on directories (not files). Violations are rejected at pack time
(`src/packers/zip_with_dockerfile.py` → `declarations_for_service`).

### Identity & authorization

A share is identified by `H(parent_instance_id, export_path)`
(`share_id` in `src/utils/shared_filesystems.py`). A child derives the **same**
id from its own `father_id` plus the guest path it declares, so it can only ever
attach to a directory *its own parent* exported at that path — it cannot address
another parent's share.

### Node placement

If a service declares any `guest` directory it **must** run on the same node as
its parent (the export is materialized locally). `launch_service` detects this
via `service_requires_parent_colocation()` and pins execution to the local node,
skipping delegation. A service that declares none is unaffected.

### Materialization (VirtioFS — implementation detail)

On the Cloud Hypervisor backend the share is materialized with VirtioFS
(`src/virtualizers/ch/virtiofs.py`), entirely invisible to the service spec:

1. One `virtiofsd` daemon per share on the host, exporting the share's host
   directory over a Unix socket keyed by the share id (`--sandbox chroot`,
   deny-by-default). The exporting parent and every co-located child reuse it.
2. One `--fs tag=…,socket=…` virtio-fs device per share on the guest's CH
   command line.
3. A guest mount plan (`/.__nodo_virtiofs`, a JSON list of `{tag, path, ro}`)
   injected into the rootfs; guest init mounts each entry (`-o ro` for `ro`).
4. Reference-counted teardown on VM kill: the daemon stops only when the last VM
   using the share on the host is gone, and the exported directory is removed
   only when the **exporting parent** departs — a child detaching never deletes
   its parent's data.

The service specification never mentions VirtioFS; swapping the backend requires
no protocol change.
