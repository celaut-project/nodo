# Policies

A **policy** is a setting of a service that a node enforces: a shared directory, a
read-only root filesystem. It is the message `celaut.Policy` (`tags`, `prose`, `formal`,
`value`), in the field `policies` of `Container.Filesystem.ItemBranch`,
`Container.Filesystem` and `Container.Init` (#670).

## Rules

- A setting is identified **only** by the exact bytes of its `formal`. `tags` and `prose`
  are for a reader; a node never uses them to identify a setting. A policy with an empty
  `formal` is invalid.
- A node that does not know the `formal` of a policy, or does not understand its `value`,
  or finds it in a place where it is not defined, **does not run the service**. An old
  node never opens what an author closed. The error names the policy by its tags (or says
  that it has none) and the place in the service.
- In one `policies` list, two policies do not have the same `formal`. A sender sorts the
  list by `formal` bytes, ascending. A reader refuses a repeated `formal`. The order does
  not change the meaning.
- A published `formal` never changes. A new meaning is a new `formal`, therefore a new
  setting.
- The `xattrs` of an entry are file attributes (mode, uid, gid, mtime, device). A node
  reads no setting from them.

The places are: a filesystem entry (`filesystem.directory` for a directory,
`filesystem.file` for the rest), the root filesystem (`filesystem.root`: the one that
`Container.filesystem` holds), and `Init` (`init`). A subdirectory's `Filesystem` has no
policies. No setting is defined on `Init` yet, so a node refuses any policy there. The
list exists now so that a setting defined later is safe on old nodes.

## Settings

The `formal` of each setting is the file with the same name here, and is the exact bytes
of the field. The code of the node holds the same bytes, and a test pins them to these
files.

| Setting | `formal` | Place | Value |
| --- | --- | --- | --- |
| `share` | [`share.formal`](share.formal) | a directory entry | `key=value` lines, sorted by key |
| `read_mode` | [`read_mode.formal`](read_mode.formal) | the root filesystem | `ro` or `rw` |

### `share`

Declares a shared directory. The value is `key=value` lines, sorted by key, UTF-8, joined
by a newline, with no empty or repeated key. Keys: `role` (required: `shared`, `guest` or
`network`), `access` (`ro` or `rw`, default `rw`), `tag`, `env`, and `network` (only with
`role=network`). Another key makes the value not valid. The rules of a share are in
[SHARED_FILESYSTEMS.md](../SHARED_FILESYSTEMS.md).

Example value of a read-only guest: `access=ro`, `env=DATASET`, `role=guest`,
`tag=photos`, one for each line.

### `read_mode`

Declares if the root filesystem is writable: `ro` or `rw`. With no policy, it is writable.
See [PACKING.md](../PACKING.md#read_mode--a-read-only-rootfs).
