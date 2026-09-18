# service.json
## Structure

```json
{
  "architecture": "linux/amd64",
  "read_only_filesystem": true,
  "init": {
    "entry_path": ["service", "start"],
    "xattrs": {
      "boot_mode": "prod"
    }
  },
  "config_declaration": {
    "path": ["config", "runtime", "node.pb"]
  },
  "api": [
    {
      "port": 8080,
      "transport": ["tcp"],
      "protocol": ["http"],
      "mu_per_call": {
        "health": "1",
        "infer": "50"
      }
    }
  ],
  "resources": {
    "at_init": {
      "mem_limit": 10000000,
      "disk_space": 2000000000
    },
    "at_most": {
      "mem_limit": 50000000,
      "disk_space": 4000000000
    }
  },
  "possible_environment_workload": [
    {
      "workloads": [
        {
          "count": 2,
          "resources": { "mem_limit": 5000000000 },
          "dependency": {
            "hash": ["0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"]
          }
        },
        {
          "count": 1,
          "resources": { "mem_limit": 40000000000 },
          "dependency": {
            "service": {
              "container": {
                "resources": {
                  "at_init": { "mem_limit": 40000000000 },
                  "at_most": { "mem_limit": 48000000000 }
                }
              }
            }
          }
        }
      ]
    },
    {
      "workloads": [
        { "count": 4, "resources": { "mem_limit": 16000000000 } }
      ]
    }
  ],
  "network": [
    {
      "tags": ["example.com"],
      "prose": "Outbound access to example.com APIs."
    }
  ],
  "envs": ["API_KEY"]
}
```

## Notes

- `init.entry_path` is serialized to `container.init.entry_path`.
- Legacy `entrypoint` is still accepted in `service.json` and is mapped to `container.init.entry_path`.
- If `service.json` provides slash-based input (for example `"/service/start"`), packer normalizes it to segmented form (`["service","start"]`).
- `init.xattrs` is serialized to `container.init.xattrs` (UTF-8 for text values).
- `read_only_filesystem` (boolean, optional, default `false`) is serialized to
  `container.filesystem.xattrs["read_mode"] = "ro"` — the xattr map on the **filesystem
  itself**, not on one of its entries, and only on the root tree that
  `container.filesystem` points at. A nested `Filesystem` (a subdirectory, reached via
  `ItemBranch.item.filesystem`) is not separately mounted, so nothing is written there.
  It declares that the service needs nothing writable beyond `/tmp` and `/run`, which
  are tmpfs; the node then builds it as an immutable erofs/squashfs image instead of a
  pre-sized ext4, and `at_init.disk_space` becomes a ceiling rather than a floor. Which
  of the two formats is used is the node's own choice and is deliberately not
  expressible here. See `docs/PACKING.md` for the full contract.
  - **Absent and `false` are identical**: no `read_mode` key is written, rather than
    `"rw"`. Absent already means `rw` to every reader, and the tree is hashed into the
    service id, so writing the default would change the id of every existing service on
    its next repack.
  - The value must be a JSON boolean. `"true"` as a string is a packer error naming the
    field, raised when `service.json` is read and before the image is built — coercing
    it would mean treating `"false"` as truthy too.
  - **Refused with `read_only_filesystem: true`:** exporting a shared filesystem (a
    directory whose `branch.xattrs` carry `shared=true`). A share is seeded from the
    exporter's own image with `debugfs`, an ext4 reader that cannot open an
    erofs/squashfs image. Importing one (`guest=true`) is fine. The packer refuses this
    at pack time, the same condition the node checks at launch.
  - **Per-entry metadata** (the contract below) is mandatory for `ro`. This packer
    already emits every key on every branch for every service, so there is nothing
    extra to declare; it is asserted before the xattr is set.
- `config_declaration.path` is serialized to `container.config_declaration.path`.
- If `service.json` provides slash-based input (for example `"/config/runtime/node.pb"`), packer normalizes it to segmented form.
- `api[].transport` is required and serialized to `api.slot[].transport.tags` (host transport, e.g. `tcp`, `udp`).
- `api[].protocol` is serialized to `api.slot[].protocol_stack[*].tags` (application protocol stack over transport). It may also be the full tags/prose/formal object form, read by the same descriptor parser as `network[].protocol_stack`.
- `api[].mu_per_call` is serialized to `api.slot[].mu_per_call` (amounts in MU, the node's unit of account).
- `network[].formal` is a flat object of **string** key/value pairs, serialized to
  `Service.Network.formal` as the sorted `key=value` body (`node_identity.component_formal`).
  Non-string values are refused, not stringified. When the entry also carries a `pow:*` tag,
  the body is run through `src/manager/pow_networks.parse_pow_formal` at pack time, so a
  malformed ask fails here instead of at launch; keys outside the `pow.` vocabulary are
  preserved, not refused. `network[].protocol_stack[]` entries are tags/prose/formal
  descriptors. Absent, both serialize exactly as before — the spec is hashed into the
  service id. See `docs/PACKING.md` and `docs/NETWORKS.md`.
- `resources.start_time_ms` no longer exists.
- `possible_environment_workload[]` declares the **worst-case descendant workloads** the service
  may request during its lifetime, for scheduling admission decisions. It is serialized
  directly to `Service.possible_environment_workload`, outside `Service.Container`. Each entry is **one independent concurrent execution
  scenario** — scenarios are *not* cumulative and imply *no* temporal ordering; a scheduler
  only checks whether each scenario, in isolation, could be satisfied. Each scenario's
  `workloads[]` item is `count` (number of concurrent descendant instances) × `resources`
  (a `Sysresources`: `mem_limit`, `disk_space`, `cpu_period`, `cpu_quota`, `blkio_weight`;
  bytes / microseconds; an omitted field defaults to `0` = no limit). Unlike `resources`
  (this instance's own needs), these describe its descendants. At launch (`launch_service`),
  every group that declares `resources` is checked for existence — every limit it declares,
  not just memory — with local admission first, then known peers via
  `GetResourceAvailability` if `network.DELEGATE_EXECUTION` is on, and the service is refused
  if a group has nowhere that could take it
  (`src/utils/cost_functions/workload_admission.py`). This proves existence, not a capacity
  reservation for `count` concurrent instances. How much is probed, and whether a group that
  fits nowhere refuses the launch or only warns, are the two `workload_admission` settings in
  `config.yaml`.
- `workloads[].dependency` is optional (or may be `null`). It can identify the descendant
  with `hash`, embed a full or partial protobuf-shaped `service`, and independently declare
  whether the embedded service is complete (`is_completed`) or whether the complete artifact
  already exists in the parent filesystem (`on_filesystem`). Hash values use hexadecimal;
  hash type defaults to `sha3_256` and can be selected explicitly with
  `{ "type": "sha3_256", "value": "<hex>" }`.
- A nested `dependency.service` uses the protobuf `Service` JSON shape. In particular,
  resources belong at `service.container.resources`, while descendant workload declarations
  remain at `service.possible_environment_workload`.

## Filesystem Metadata Xattrs Contract

Each `container.filesystem.branch[*]` element includes a metadata contract in
`branch.xattrs` so Cloud Hypervisor can rebuild an `ext4` with the original
filesystem metadata.

Reserved keys:

- `mode`
- `uid`
- `gid`
- `mtime_ns`
- `device.major`
- `device.minor`
- `device.is_block`

Encoding rules:

- All values are UTF-8 bytes containing base-10 integers.
- `device.is_block` must be `0` or `1`.
- Non-device elements (`file`, `dir`, `symlink`) must encode:
  - `device.major = 0`
  - `device.minor = 0`
  - `device.is_block = 0`

Compatibility behavior:

- Legacy services without these keys are still accepted by Cloud Hypervisor
  builder, using the previous executable fallback heuristic.
- Partial or malformed metadata is treated as an integrity error.
- The fallback heuristic is **not** available to a `read_only_filesystem` service:
  it cannot restore a uid or gid and cannot produce a device node, and a read-only
  image offers no way to correct either from inside the guest. For those services
  every key above is required on every entry, which this packer always emits.
