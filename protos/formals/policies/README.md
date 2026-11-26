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
- The keys of the `xattrs` of an entry (mode, uid, gid, mtime, device) are defined by the
  setting [`entry_metadata`](entry_metadata.formal) of the root filesystem. Without that
  setting, a node reads none of them, and it refuses a tree where an entry carries one of
  the keys. A node reads no other setting from `xattrs`.

The places are: a slot of the API (`api.slot`, #669), a filesystem entry (`filesystem.directory` for a directory,
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
| `entry_metadata` | [`entry_metadata.formal`](entry_metadata.formal) | the root filesystem | empty |
| `access` | [`access.formal`](access.formal) | a slot of the API | `direct`, `tunnel` or `any` |
| `tunnel_auth` | [`tunnel_auth.formal`](tunnel_auth.formal) | a slot of the API | `token` or `address` |
| `wake` | [`wake.formal`](wake.formal) | a slot of the API | `never` or `on_tunnel` |

### `share`

Declares a shared directory. The value is `key=value` lines, sorted by key, UTF-8, joined
by a newline, with no empty or repeated key. Keys: `role` (required: `shared`, `guest` or
`network`), `access` (`ro` or `rw`, default `rw`), `tag`, `env`, and `network` (only with
`role=network`). Another key makes the value not valid. The rules of a share are in
[SHARED_FILESYSTEMS.md](../../../docs/SHARED_FILESYSTEMS.md).

Example value of a read-only guest: `access=ro`, `env=DATASET`, `role=guest`,
`tag=photos`, one for each line.

### `entry_metadata`

Declares that the tree carries metadata in `ItemBranch.xattrs`. The value is empty: any
other value is not valid. The keys are `mode`, `uid`, `gid`, `mtime_ns`, `device.major`,
`device.minor` and `device.is_block`, as base-10 integers in UTF-8 text. An entry has all
the keys or none. An entry with none gets the default of the node. `read_mode=ro` needs
this setting and all the keys on every entry. A node that finds one of the keys in a tree
without the setting does not run the service: the service must be packed again. The packer
always writes this setting. See [PACKING.md](../../../docs/PACKING.md#entry_metadata--per-entry-metadata).

### `read_mode`

Declares if the root filesystem is writable: `ro` or `rw`. With no policy, it is writable.
See [PACKING.md](../../../docs/PACKING.md#read_mode--a-read-only-rootfs).

### Settings of a slot

Three settings of one slot of `Service.Api` (#669). The value is the word, in UTF-8. A slot
that does not set one has the default.

| Setting | Default | Meaning |
| --- | --- | --- |
| `access` | `any` | How a third party reaches the slot. The node enforces it in the tunnel only. A direct connection does not go through the node, so for it `access=tunnel` is a declaration for callers. |
| `tunnel_auth` | `token` | How the tunnel finds the instance. `address`: the `Instance.Uri` that the caller knows, with no token. |
| `wake` | `never` | `on_tunnel`: a tunnel connection wakes a hibernated instance. |

A node refuses a service that has `wake=on_tunnel` or `tunnel_auth=address` on a slot with
`access=direct`. The node ignores these settings in `ConfigurationFile.gateway`. A packer
writes them from `policies` in a slot of `api` in `service.json`:
`{"port": 8333, "policies": {"tunnel_auth": "address", "wake": "on_tunnel"}}`. How the tunnel
uses them is in [TUNNELING.md](../../../docs/TUNNELING.md).
