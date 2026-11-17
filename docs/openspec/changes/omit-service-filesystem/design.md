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
- On: receive the complete stream, hash it, and do not persist the filesystem blocks. The
  transfer costs the full bandwidth. The disk is not used for the filesystem. The result is
  a verified id. If bee-rpc cannot receive a block without it writing the block, the
  acquisition writes the block to a temporary place and removes it when the block ends. The
  peak disk use is then one block, not the whole filesystem.
- Off: ask the sender to skip the filesystem block. This saves bandwidth. The id cannot be
  checked over the omitted part, so the node says so.

**One helper for the state.** A function in `src/utils` answers "is this service partial".
The node code does not look for `.nofs` in other places.

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
  The default is on.
- [Verified acquisition downloads hundreds of gigabytes to store nothing] → This is the
  price of the check. The operator can turn the check off, and then the skip saves the
  transfer.
- [A peer or a client expects every service in the registry to be runnable] → `GetService`,
  export and publish refuse a partial service with a clear reason. The launch error names
  the cause.
- [A crash in prune leaves a service with some filesystem blocks removed and no `.nofs`] →
  Write `.nofs` before the first block is removed. A service with `.nofs` and some blocks
  still on disk is valid and prune can finish it.
- [A node that quotes a partial service attracts delegated work that it cannot run] → Open
  question below.

## Open Questions

- Which section holds the key: `main.OMIT_FS` (assumed here) or another one. This changes
  only the name, not the behavior.
- Does the packer (`nodo pack`) keep or drop the filesystem of the services that it builds
  when `main.OMIT_FS` is `true`? Assumed: it keeps them, because the node must run the build.
  This can be revisited without a change to the specs above.
- Does `GetServiceEstimatedCost` from a peer answer for a partial service? Assumed: it
  answers only if the node can run the service or delegate it, as it does today.
