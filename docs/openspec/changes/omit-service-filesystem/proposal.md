# Proposal

Issue: none

## Why

A service can need hundreds of gigabytes, nearly all of it in its filesystem. A small device
cannot store that. The node needs a mode to keep the specification of a service and not its
filesystem. The node then still knows the service, prices it and starts it on a peer. This
is not possible today: the registry holds only complete services.

## What Changes

- Add the option `misc.OMIT_FS` (default `false`, in the `misc` section next to `VALIDATE_ON_IMPORT`). When it is `true`, the node does not keep
  the filesystem of a service that it imports, downloads or gets from a peer.
- Add the marker file `.nofs` next to `_.json` in the registry entry of a service. The marker
  says: this service is partial. A partial service has its specification and no filesystem.
- With `misc.VALIDATE_ON_IMPORT` on, the node reads the complete stream, checks the service
  id, and does not store the filesystem blocks. With it off, the node skips the filesystem
  block with the block-skip of bee-rpc.
- A node does not run a partial service itself. It can delegate the service to a peer, as it
  does today, because the delegation sends only hashes.
- A node does not give a partial service to a peer (`GetService`), and does not export or
  publish it.
- `nodo download` treats a partial service as a service that the node does not hold in full.
  With `misc.OMIT_FS` off, it completes the service and removes the marker.
- `nodo storage:prune_blocks`, with `misc.OMIT_FS` on, removes the filesystem of each complete
  service and writes `.nofs`. A service with a live local instance keeps its filesystem.
  Setting the option alone does not remove anything.
- A service with no block stays complete: there is nothing to omit.

Out of scope: `nodo execute --omit-download` (rejected: the node cannot apply its own
network policy or price the service without the specification), and any change to the
service id or to the `.celaut.bee` format.

## Capabilities

### New Capabilities
- `registry/partial-services`: how the node stores, marks, serves, completes and prunes
  services without their filesystem.

### Modified Capabilities

None. The main specs in `docs/openspec/specs/` are empty now.

## Impact

- Code: `import_bee`, the publisher download path, `acquire_service`, `storage:prune_blocks`,
  `GetService`, local execution in `launch_service`, `export_bee`, `export_raw`, `publish`,
  `integrity`.
- Config: one new key, `misc.OMIT_FS`, in `docs/CONFIG.md` and `config.example.yaml`. The
  key list of `compat.json` changes, so `nodo update` sees a config key change.
- Dependencies: none. bee-rpc cannot receive a block and not store it, so the node patches it
  at run time (see design). A change in bee-rpc itself would replace the patch.
