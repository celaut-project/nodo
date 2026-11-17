# Design

## Context

A service in the registry is a bee-rpc multiblock directory. `REGISTRY/<id>/_.json` lists the
parts. The specification is a file with block pointers. The blocks are in `BLOCKDIR`, shared
and addressed by content. `Container.filesystem` is a serialized `Filesystem` or a pointer to
a block. The large files inside are blocks too.

The service id is the hash of the complete content stream, filesystem included
(`read_service_content`). Thus a node cannot check the id of a service if it drops the
filesystem before it reads it.

The specification already loads without the blocks (`load_service_from_disk`).
`block_tree` and `storage:prune_blocks` accept a block that is not on disk. bee-rpc has a
block-skip on the sender side (`skip_requested_blocks`). Delegation to a peer sends only
hashes, so a partial node can still delegate.

## Goals / Non-Goals

**Goals:**
- Let a small device keep the specification of a service and not its filesystem.
- Keep the check of the service id when the operator wants it.
- Keep one test for "is this service partial" for the whole node.

**Non-Goals:**
- `nodo execute --omit-download`. The node needs the specification to apply its network
  policy and to price the service. Rejected.
- A new service id, or a new `.celaut.bee` format.
- Run a partial service on the same node.

## Decisions

**Marker file `.nofs`.** The state is a file next to `_.json`. Alternatives: a field in the
metadata (the metadata can be replaced on import, and a source could change it), or a table
in the database (a second source of truth that can differ from the disk). The file is next to
the data it describes and is easy to check.

**Two acquisition paths, set by `misc.VALIDATE_ON_IMPORT`.**
- On: receive the complete stream, hash it, and do not store the filesystem blocks. The
  transfer costs the full bandwidth. The disk is not used for the filesystem. The result is
  a verified id. The node asks no skip (`block_skip` is off), because a body that the peer
  skips cannot be hashed.
- Off: ask the sender to skip the filesystem block (`StreamControl.request_skip`). This
  saves bandwidth. The id cannot be checked over the omitted part, so the node says so.

**The filter is a session over the raw stream, plus a patch of bee-rpc.** bee-rpc stores each
block while it reads, and it needs the length of every block on disk to write the pointer
form of the service (`wbp.bin`, the file that `load_service_from_disk` reads). The
alternatives were a change in bee-rpc (a release outside this repository) or a sparse
placeholder file of the right length (a block that exists with false content). The filter
in `src/utils/omit_filesystem.py` does this:
- A tap over the buffers follows the block markers. A block that is directly in the
  service stream is a "depth-1 block". Every such block is part of the filesystem: the
  packer stores the filesystem as one block, and the older inline shape as one block per
  large file. The service has no other block.
- The length of a depth-1 block is read from the varint that comes just before it in the
  bytes of the service. It is the same when the sender skips the body, so one rule serves
  both paths.
- With verification, the tap hashes each chunk of the service partition.
- A patch of `save_chunks_to_block` reads a depth-1 block off the stream, stores nothing,
  and records the pointer in `_.json`. A patch of the two size functions gives bee-rpc the
  length of the missing block while it writes the pointer form. The patches act only inside
  a session of the same thread. The `StreamControl` of the call is captured the same way.

The tap needs the stream before bee-rpc parses it. `import_bee` calls `parse_from_buffer`
itself. For `GetService`, `BeeClient.get_service` wraps the stub method.

**One helper for the state.** `src/utils/partial_service.py` answers "is this service partial". The node code does not ask the disk for `.nofs` anywhere else, except the integrity check, which reads the marker name from it.

**Local candidate excluded, not an error.** `launch_service` already skips the local
candidate for a missing `virtiofsd` and records a reason. A partial service uses the same
mechanism. The delegation path is unchanged.

**Prune is the only action on existing data.** Setting the option does not delete anything.
`nodo storage:prune_blocks` takes the filesystem blocks of each complete service (the blocks
that are reachable from `Container.filesystem`), removes those that no other part of any
service needs, and writes `.nofs`. A block that is also part of another specification stays.

**Download completes a partial service.** The existing "already in the registry" check in
the acquisition path must ask if the service is complete, not if it exists.

## Risks / Trade-offs

- [The id is not verified when `VALIDATE_ON_IMPORT` is off] → The node warns the operator.
  The default is on. Note: the path that gets a service from a peer verifies the id today
  even with `VALIDATE_ON_IMPORT` off. With `OMIT_FS` on and the check off, it does not.
- [Verified acquisition downloads hundreds of gigabytes to store nothing] → This is the
  price of the check. The operator can turn the check off, and then the skip saves the
  transfer.
- [A download from a source writes the `.celaut.bee` artifact to disk before the import]
  → The node needs the disk for that file for a short time. Only the transfer from a peer
  streams with no file. A source is a plain HTTP download, and streaming it into the parser
  is a separate change.
- [The patch of bee-rpc depends on its internals (`save_chunks_to_block`, the size
  functions, `StreamControl`)] → The tests build real services and run real gRPC calls. The
  version of bee-rpc is pinned. A change in bee-rpc itself would remove the patch.
- [A peer or a client expects every service in the registry to be runnable] → `GetService`,
  export and publish refuse a partial service with a clear reason. The launch error names
  the cause.
- [A crash in prune leaves a service with some filesystem blocks removed] → `.nofs` is
  written before the first block is removed. A service with `.nofs` and some blocks still
  on disk is valid and the next prune finishes it.
- [A running instance needs the filesystem when the node revives it] → Prune keeps a service
  that has a live local instance complete. If the database cannot be read, prune drops
  nothing.
- [A node that quotes a partial service attracts delegated work that it cannot run] → The
  quote has no local offer for a partial service. It can still offer a peer's price.
- [The key list of `compat.json` changes] → `nodo update` sees a config key change. The key
  has a default, so an old `config.yaml` works.

## Decisions taken while building

- The key is `misc.OMIT_FS`, next to `misc.VALIDATE_ON_IMPORT`. A key in `main` is judged by
  the installer as a path.
- `nodo pack` is not changed. The services that it builds stay complete.
- A service with no block stays complete.
- A partial service shows no declared share or guest directory: the node that runs it reads
  its own filesystem and judges them.
